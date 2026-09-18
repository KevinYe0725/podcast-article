"""知识库与记忆：把「一堆各自独立的文章」变成「一个可以问、可以追溯、记得住的书库」。

为什么要它：现在书库是「一篇一篇」的——检索只在正文里找词，助手只认当前这一集，
今天问过的问题明天再问还要再问一遍。知识库把三件事补上：

    1. 跨集检索：一个问题的答案可能散在五集里，能一次捞出来并给出处（哪一集、第几分钟）
    2. 实体索引：谁（人 / 机构 / 书影音）在哪些集里出现过，各自说了什么
    3. 记忆：用户偏好、结论、决定，能记住、能改、能删、能被助手读到

技术选型（都要求**本地、开源、无额外账单**）
- **SQLite + FTS5** 做词法检索：单文件、零运维，跟 output/ 一起备份就行
- **jieba** 做中文分词：SQLite 自带的分词器不切中文，整句当"一个词"等于没有检索
- **fastembed（ONNX）** 做向量检索，**可选**：不需要 torch，模型约 100MB
- **numpy 暴力算余弦**：个人级书库（几万条以内）根本不需要向量数据库，
  一次矩阵乘法就够了；少一个服务、少一堆故障点

没有嵌入模型时**功能不残缺**：退回纯词法检索（中文分词 + BM25），语义近似改写会弱一些，
状态里会如实写明当前是哪种模式。

数据库位置：默认 `PROJECT_ROOT/kb.sqlite`，可用 `PA_KB_FILE` 覆盖。
它是**派生数据**——随时可以删掉重建（`reindex`），所以不做复杂迁移：schema 版本变了就重建。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

from .config import PROJECT_ROOT

SCHEMA_VERSION = 3

# 版本升级时可以随手重建的表（派生数据，能从 output/ 重算）。
# **memory / memory_fts 不在其中**：记忆是用户写下的原话，重算不出来。
_DERIVED_TABLES = ("passage_entities", "passages_fts", "passages", "entities",
                   "embeddings", "docs")
DB_PATH = Path(os.environ.get("PA_KB_FILE") or (PROJECT_ROOT / "kb.sqlite"))
EMBED_MODEL = os.environ.get("PA_KB_EMBED_MODEL") or "BAAI/bge-small-zh-v1.5"
PASSAGE_CHARS = 420          # 单条切片的目标长度（检索粒度：太小丢上下文，太大降精度）
MIN_PASSAGE_CHARS = 12
# 这里的取舍：下限是为了挡「孤立标题、一个词、一句话的残片」这类噪声，但设成 30 会连
# **一条正常但很短的正文句子**一起删掉 —— 那句话就永远搜不到了（真实踩过：一篇 12 集的库
# 里有整节被丢）。12 字足以滤掉碎片，同时保住「小标题下只有一句话」这种正常写法。
# 语义检索的相似度下限（余弦）。低于它的结果直接丢掉，宁可不搜也不给噪声。
# 这个数是**在真实书库上标定**的（12 集 / 3890 切片，bge-small-zh-v1.5）：
#   该命中的查询 top1 = 0.602 ~ 0.773（"推理芯片的成本" 0.773、"孙正义怎么押注" 0.691）
#   不该命中的 top1 = 0.439 ~ 0.564（"量子纠缠的贝尔不等式" 0.564、"qqqzzzxyzzy" 0.531）
# 取 0.58 正好把两组分开，同时相关查询仍能返回 2-3 条（0.58~0.60 那几条还在）。
# 只影响**向量**命中：词法命中不受它约束，所以关键词能搜到的东西不会因此丢。
# 换嵌入模型后必须重新标定；可用 PA_KB_SEM_MIN 覆盖。
SEM_MIN_SIM = float(os.environ.get("PA_KB_SEM_MIN") or 0.58)

MEMORY_KINDS = ("preference", "fact", "entity", "decision", "insight")
ENTITY_TYPES = ("person", "org", "media", "place", "topic")


# ============================================================ 分词

_JIEBA = None
_STOP = set("""
的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这
我们 他们 它 他 她 而 与 及 或 但 并 等 被 把 让 给 对 从 向 里 中 时候 什么 怎么 为什么
the a an of to in is are was were be been it this that and or for on with as at by from
""".split())


# 一份很小的用户词典：AI / 播客语境里的常见专名，jieba 的通用词典不认（实测「英伟达」
# 会被切成 英/伟达，检索时就永远命中不到整词）。词条少而准，不做大而全。
# (词, 词性)：词性必须显式给，否则 add_word 会把这些词标成普通名词，
# 「孙正义」就不再是 nr —— 实体抽取会连带失效（实测踩过这个坑）
_USER_DICT: list[tuple[str, str]] = [
]
_NAMED = {
    "nr": "孙正义 黄仁勋 马斯克 奥特曼 朱邦华 梁文锋 任正非 藤田田 赵长鹏 盛颖",
    "nt": "英伟达 台积电 软银 纽交所 纳斯达克 微软 谷歌 字节跳动 阿里巴巴 腾讯",
    "nz": ("大模型 强化学习 推理芯片 算力 对齐 微调 蒸馏 上下文 智能体 多模态 "
           "小宇宙 播客 文字稿 时间戳 订阅 音频 转写 知识库"),
}
_USER_DICT = [(w, tag) for tag, words in _NAMED.items() for w in words.split()]


def _jieba():
    """懒加载 jieba（它的词典加载要几百毫秒，别拖慢 Web 启动）。"""
    global _JIEBA
    if _JIEBA is None:
        import jieba
        jieba.setLogLevel(60)          # 关掉它往 stderr 打的构建词典日志
        # 先给「通用词典」拍快照再喂用户词：add_word 会给词一个几百的建议频次，
        # 直接读 jieba.dt.FREQ 会把真名误判成常用词（实测踩到，实体因此全丢）。
        _FREQ_SNAPSHOT.update(dict(jieba.dt.FREQ))
        for word, tag in _USER_DICT:
            jieba.add_word(word, tag=tag)
        _JIEBA = jieba
    return _JIEBA


def tokenize(text: str, *, keep_stop: bool = False) -> str:
    """切词并返回空格分隔的串（直接交给 FTS5 的 unicode61 分词器）。

    FTS5 默认不切中文，所以必须自己切好再存 —— 这是中文全文检索的关键一步。
    英文/数字保持原样并转小写，长词不再二次切分。
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return ""
    out: list[str] = []
    for word in _jieba().cut(text):
        w = word.strip().lower()
        if not w or w in _STOP:
            continue
        if len(w) == 1 and not w.isalnum():        # 单字标点/符号丢掉
            continue
        if len(w) == 1 and not re.match(r"[\u4e00-\u9fff0-9a-z]", w):
            continue
        out.append(w)
    return " ".join(out)


# ============================================================ 连接与建表

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    dir TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- article / transcript / qa
    title TEXT DEFAULT '',
    podcast TEXT DEFAULT '',
    url TEXT DEFAULT '',
    mtime REAL DEFAULT 0,
    sha TEXT DEFAULT '',
    passages INTEGER DEFAULT 0,
    indexed_at REAL DEFAULT 0,
    UNIQUE(dir, kind)
);
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    kind TEXT DEFAULT 'body',           -- heading / body / quote / takeaway / qa
    heading TEXT DEFAULT '',
    start_sec REAL,
    end_sec REAL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passages_doc ON passages(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(tokens, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS embeddings (
    passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    count INTEGER DEFAULT 0,
    note TEXT DEFAULT '',               -- 一句话说明（LLM 精修时填，人工也能改）
    source TEXT DEFAULT 'heuristic'     -- heuristic / llm / manual
);
CREATE TABLE IF NOT EXISTS passage_entities (
    passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (passage_id, entity_id)
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    tags TEXT DEFAULT '',
    source_dir TEXT DEFAULT '',
    source_kind TEXT DEFAULT '',        -- user / assistant / article / manual
    weight REAL DEFAULT 1.0,
    pinned INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0,
    -- 用过的次数与最后一次使用时间：用来判断「哪条记忆其实一直没在用」。
    -- 记忆烂掉的第一步是「堆了一堆谁也不看的东西」，而这件事只有用量看得见。
    use_count INTEGER DEFAULT 0,
    last_used_at REAL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(tokens, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db_path() -> Path:
    return DB_PATH


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    """建表；schema 版本变了就重建**派生表**，并把 FTS 表修成可删版本。

    两条必须守住的规矩：

    1. **`memory` 永远不 DROP。** 知识库是派生数据，重建比迁移便宜；但记忆不是派生数据
       —— 它是用户自己写下的东西，丢了没有任何办法重建。曾经这里把整个库（含 memory）
       一起 drop，等于「升个版本，你的记忆就没了」。现在只重建派生表，记忆原地保留。
    2. FTS 表如果是 **contentless**（`content=''`），SQLite 不允许 DELETE：
       `cannot DELETE from contentless fts5 table`。老版本建的库就是这样，表现为
       **删记忆 500**、**改过的文章一重建索引就报错**。所以这里检测 DDL 并就地修好
       （drop 掉 virtual table 重建为普通 FTS5，再从原文表灌一遍 tokens —— 能重算的都不怕）。
    """
    have = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    version = None
    if have:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = row["value"] if row else None
    stale = version is not None and int(version) != SCHEMA_VERSION
    if stale:
        for t in _DERIVED_TABLES:            # 只清派生表；memory / memory_fts 不在其中
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(_SCHEMA)
    _migrate_memory_columns(conn)
    _repair_fts(conn)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _fts_is_contentless(conn: sqlite3.Connection, name: str) -> bool:
    """这张 FTS 表是不是不带 content 的（= 不能 DELETE）。

    只看 DDL 里有没有 `content=`（引号空格都去掉再比）：普通 FTS5 表不写这个参数，
    contentless 的写法是 `content=''`。`content_rowid=` 不会误命中（下划线挡住了）。
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    sql = (row["sql"] if row else "") or ""
    flat = sql.replace(" ", "").replace("'", "").replace('"', "")
    return "content=" in flat


def _repair_fts(conn: sqlite3.Connection) -> None:
    """把 contentless 的 FTS 表就地换成可删的普通表，并从原文表重灌 tokens。"""
    if _fts_is_contentless(conn, "passages_fts"):
        conn.execute("DROP TABLE IF EXISTS passages_fts")
        conn.execute("CREATE VIRTUAL TABLE passages_fts USING fts5(tokens, tokenize='unicode61')")
        for r in conn.execute("SELECT id, heading, text FROM passages").fetchall():
            conn.execute("INSERT INTO passages_fts(rowid, tokens) VALUES(?,?)",
                         (r["id"], tokenize(f"{r['heading'] or ''} {r['text'] or ''}")))
    if _fts_is_contentless(conn, "memory_fts"):
        # 记忆的 tokens 能从 memory.text 重算 —— 所以这张表坏了也修得回来，
        # 真正不可重建的只有 memory 表本身。
        conn.execute("DROP TABLE IF EXISTS memory_fts")
        conn.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(tokens, tokenize='unicode61')")
        for r in conn.execute("SELECT id, text FROM memory").fetchall():
            conn.execute("INSERT INTO memory_fts(rowid, tokens) VALUES(?,?)",
                         (r["id"], tokenize(r["text"] or "")))


# ============================================================ 切片

_HEADING = re.compile(r"^(#{1,6})\s*(.+)$")
_QUOTE = re.compile(r"^>\s?(.+)$")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)、])\s+(.+)$")
_TS_RANGE = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*[-–—~～至到]\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?\]")


def _sec(h: str, m: str, s: str | None) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s or 0)


def _split_article(text: str) -> list[dict]:
    """文章 → 带小标题上下文的切片。

    按「小标题 + 段落」组织：每个切片都记住自己属于哪个小标题，检索命中后能说清
    「在《…》这一集的‘他为什么死守两条红线’一节里」。
    """
    out: list[dict] = []
    heading = ""
    buf: list[str] = []
    buf_kind = "body"

    def flush() -> None:
        nonlocal buf, buf_kind
        body = "\n".join(buf).strip()
        buf = []
        if len(body) < MIN_PASSAGE_CHARS:
            return
        # 长段落按句号再切，保证每条切片都在目标长度附近
        if len(body) <= PASSAGE_CHARS * 1.6:
            out.append({"text": body, "kind": buf_kind, "heading": heading})
            return
        piece = ""
        for sent in re.split(r"(?<=[。！？!?])", body):
            if len(piece) + len(sent) > PASSAGE_CHARS and piece:
                out.append({"text": piece.strip(), "kind": buf_kind, "heading": heading})
                piece = sent
            else:
                piece += sent
        if piece.strip():
            out.append({"text": piece.strip(), "kind": buf_kind, "heading": heading})

    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            flush()
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            heading = m.group(2).strip()
            out.append({"text": heading, "kind": "heading", "heading": heading})
            continue
        q = _QUOTE.match(line)
        if q:
            flush()
            buf_kind = "quote"
            ts = _TS_RANGE.search(q.group(1))
            start = _sec(ts.group(1), ts.group(2), ts.group(3)) if ts else None
            end = _sec(ts.group(4), ts.group(5), ts.group(6)) if ts and ts.group(4) else None
            out.append({"text": _TS_RANGE.sub("", q.group(1)).strip(), "kind": "quote",
                        "heading": heading, "start_sec": start, "end_sec": end})
            buf_kind = "body"
            continue
        b = _BULLET.match(line)
        if b:
            flush()
            out.append({"text": b.group(1).strip(), "kind": "takeaway", "heading": heading})
            continue
        buf.append(line)
    flush()
    return out


def _split_transcript(text: str, window: int = 3) -> list[dict]:
    """文字稿 → 按时间窗合并的切片（带起止秒数，检索命中后能直接跳回听）。"""
    lines = []
    for line in text.splitlines():
        m = re.match(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)$", line.strip())
        if m and m.group(4).strip():
            lines.append((_sec(m.group(1), m.group(2), m.group(3)), m.group(4).strip()))
    if not lines:
        parts = [{"text": t, "kind": "body", "start_sec": None, "end_sec": None}
                 for t in (text[i:i + PASSAGE_CHARS] for i in range(0, len(text), PASSAGE_CHARS))
                 if t.strip()]
        # 短文也要能检索：只要整篇还有内容，就至少留一条切片（长度下限只用来丢空白片段）
        return parts if len(text.strip()) >= MIN_PASSAGE_CHARS else parts[:1] if text.strip() else []
    out: list[dict] = []
    for i in range(0, len(lines), window):
        group = lines[i:i + window]
        body = " ".join(t for _, t in group).strip()
        # 短句窗口不丢：只有整篇都很短时才不会再被合并，丢掉就等于整集检索不到
        if len(body) < 10:
            continue
        out.append({"text": body, "kind": "body", "start_sec": group[0][0], "end_sec": group[-1][0]})
    return out


# ============================================================ 实体

_MEDIA = re.compile(r"《([^》]{1,40})》")
# jieba 的词性标注没有上下文模型，「东西/明白/希望」这类常用词经常被标成 ns/nr，
# 「美国/中国」这种高频国名当成实体也没信息量。这里用两张表把它压住：
#   1) 明确的误标黑名单（动词/形容词/代词被当成专名）
#   2) 通用词黑名单（出现得太普遍，指不出任何具体东西）
_ENTITY_DENY = set("""
东西 明白 希望 觉得 认为 知道 应该 可能 需要 开始 结束 时候 问题 事情 地方 方法 内容
观点 状态 情况 结果 影响 意思 感觉 想法 方式 过程 时间 全球 世界 大家 自己 别人 他们
今天 明天 昨天 现在 最近 未来 过去 中国 美国 英国 日本国 法国 德国 俄罗斯 印度 韩国
政府 公司 集团 企业 行业 市场 经济 社会 文化 历史 科技 技术 产品 服务 数据 系统 平台
能力 水平 质量 数量 价格 成本 收入 支出 投资 融资 上市 股价 员工 用户 客户 记者 编辑
""".split())


_ROLE = re.compile(r"(创始人|联合创始人|CEO|CTO|CFO|COO|总裁|主席|首席|教授|博士|研究员|工程师|"
                    r"作者|导演|制片|歌手|主播|主持|记者|编辑|律师|医生|投资人|合伙人|"
                    r"先生|女士|老师|嘉宾)")


_FREQ_SNAPSHOT: dict = {}


def _freq(name: str) -> int:
    """这个词在**通用词典**里的频次：真实人名通常极低，常用词/术语很高。

    读的是加用户词之前的快照（见 _jieba），否则我们自己塞进去的词会被当成高频词。
    """
    if not _FREQ_SNAPSHOT:
        try:
            _jieba()
        except Exception:
            return 0
    return int(_FREQ_SNAPSHOT.get(name, 0))


_CURATED = {w: tag for w, tag in []}      # 由 _NAMED 填充（见下方初始化）


# 常见姓氏（百家姓高频部分 + 少量常见日韩姓氏）。中文人名识别的**高精度**信号：
# 词典频次只能压住"太贵/白皮书"这类词，但"塞进/堵墙"频次也很低，光看频次会漏；
# 加上"首字是姓氏"这一条，就能把动宾短语挡在外面（实测有效）。
_SURNAME_TEXT = """
赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛
奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮齐康伍余
元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强
贾路娄危江童颜郭林钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫房裘缪干解应宗丁宣邓郁单杭洪包诸左
石崔吉钮龚程邢滑裴陆荣翁荀羊惠甄曲封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋
仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴
胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连习
宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾
辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公
藤佐铃
"""
# 单字姓按**字符**收集（上面是连排文本，直接 split() 会得到整行字符串）
_SURNAME_CHARS = set("".join(_SURNAME_TEXT.split()))
# 复姓与常见日韩姓氏按整词匹配
_SURNAME_MULTI = ("高桥", "田中", "山本", "中村", "小林", "加藤", "吉田", "佐藤", "铃木",
                  "渡边", "伊藤", "藤田", "藤原", "宫崎", "山口", "石川", "上野", "中山")


def _has_surname(name: str) -> bool:
    return bool(name) and (name[0] in _SURNAME_CHARS or name.startswith(_SURNAME_MULTI))


def _person_ok(name: str, context: str) -> bool:
    """人名要过三道关：不像常用词、长度像人名、且有旁证。

    旁证 = 出现在小标题里，或者紧挨着身份词（"创始人孙正义"），或者这个名字在语料里
    出现得很干脆（词典频次极低）。只有 jieba 标了 nr 是不够的 —— 实测「塞进/堵墙/太贵」
    都会被标成 nr。
    """
    if not (2 <= len(name) <= 4):
        return False
    if _freq(name) >= 15:
        return False
    if _has_surname(name):
        return True                       # 姓 + 低词频 → 基本就是人名
    if _freq(name) == 0 and len(name) >= 3:
        return True                       # 词典里完全没有的三字及以上 → 也有很大概率是名（含昵称）
    for m in re.finditer(re.escape(name), context):
        window = context[max(0, m.start() - 12):m.end() + 12]
        if _ROLE.search(window):
            return True
    return False


_CURATED = {w: t for t, words in _NAMED.items() for w in words.split()}
_TYPE_OF_TAG = {"nr": "person", "nt": "org", "nz": "topic"}


def _looks_like_entity(name: str, etype: str, *, context: str = "", heading: bool = False) -> bool:
    """过滤掉「一看就不是专名」的东西。宁可少收，也别让实体页塞满废话。"""
    name = name.strip()
    if len(name) < 2 or len(name) > 24 or name in _ENTITY_DENY or name in _STOP:
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", name):
        return False
    if re.fullmatch(r"[0-9.]+%?", name):
        return False
    # 纯英文字母且很短（of / the / ai）没意义；两字母以上大写缩写（AI / IPO / API）保留
    if re.fullmatch(r"[a-z]{2,}", name) and name.islower():
        return False
    if etype in ("org", "place", "topic") and _freq(name) >= 400:
        return False                      # 交易所/清华/东京 这类高频通用词不算实体
    if etype == "person":
        return heading or _person_ok(name, context)
    return True
_POS_MAP = {"nr": "person", "nt": "org", "ns": "place", "nz": "topic"}
_ORG_HINT = re.compile(r"(公司|集团|大学|学院|基金|实验室|研究院|银行|政府|委员会|机构|国|部)$")


def extract_entities(text: str, *, limit: int = 40) -> list[tuple[str, str, float]]:
    """从一段文字里抽实体：人 / 机构 / 书影音 / 地点 / 主题。

    用 jieba 的词性标注（nr=人名、nt=机构、ns=地名、nz=专名）加几条确定性规则，
    **不调用任何模型** —— 抽取是索引时的常规动作，不该按字计费。
    《书名》这类用书名号直接识别（播客里提及的书影音是很有价值的线索）。
    返回 [(名称, 类型, 权重)]，权重用于后面的排序。
    """
    found: dict[str, tuple[str, float]] = {}
    # 白名单直连：这些词是我们自己写在词表里的，词性由词表决定，不依赖 jieba 的标注
    raw = str(text or "")
    for name, tag in _CURATED.items():
        if name in raw:
            found[name] = (_TYPE_OF_TAG.get(tag, "topic"), 1.2)
    for m in _MEDIA.finditer(raw):
        name = m.group(1).strip()
        if 1 < len(name) <= 40:
            found[name] = ("media", 1.5)
    try:
        import jieba.posseg as pseg
        for word, flag in pseg.cut(str(text or "")):
            name = word.strip()
            etype = _POS_MAP.get(flag)
            if not etype:
                continue
            if etype == "org" or _ORG_HINT.search(name):
                etype = "org"
            if not _looks_like_entity(name, etype, context=body,
                                      heading=(c.get("kind") == "heading")):
                continue
            found.setdefault(name, (etype, 1.0))
    except Exception:
        # 词性标注失败不该让索引整个挂掉：书名号那部分照样能用
        pass
    items = sorted(found.items(), key=lambda kv: (-kv[1][1], -len(kv[0])))[:limit]
    return [(name, etype, weight) for name, (etype, weight) in items]


# ============================================================ 嵌入（可选）

_EMBEDDER = None
_EMBED_ERR = ""
_VEC_CACHE: dict = {"model": "", "ids": None, "mat": None, "db": ""}


def _vec_invalidate() -> None:
    """让进程内的向量缓存失效。

    **必须整个清掉，不能只清 ids**：只清 ids 而留下 mat，`_load_vectors` 会认为缓存
    仍然可用（它看的是 mat 与 model），于是 `_semantic` 拿去 `cache["ids"][idx]` 就炸
    —— 表现为「刚生成的文章一进库，跨集搜索就 500」。实测踩过，见 test_memory_flow。
    """
    _VEC_CACHE.update({"model": "", "ids": None, "mat": None, "db": ""})
    # 同一个写入点：切片一变，向量的 df 缓存也必须一起失效（见 _df_invalidate）。
    # 放在这里而不是让每个调用方自己记得调两次 —— 忘了任何一次都会吃到过期的 df，
    # 而「过期 df」不会报错，只会让排序悄悄错掉，是那种最难发现的坏。
    _df_invalidate()


# ---------------------------------------------------------------- 词频统计（IDF 用）

# 为什么需要它：覆盖率如果**按词计数**，两个词在公式里权重一模一样，而「命中哪几个词」
# 才是真正的差别。真库实测（问「孙正义是怎么押注 AI 的？」，N=5612 条切片）：
#   `ai`     df=382  idf=2.75
#   `孙正义` df= 79  idf=4.27
#   `押注`   df=  8  idf=6.44
# 等权口径下「只命中一个词」一律拿 0.33，于是只谈「押注」却与孙正义毫无关系的段落
# （Bloomberg 那集讲 Dario Amodei）拿到与实质证据同档的覆盖率，混进 top-10 挤掉答案。
# 加权之后，命中稀有词的切片才拿得到高覆盖率。
#
# 注意：`押注` 的 df 比 `孙正义` 还小（8 vs 79）—— **加权并不保证「专名一定赢泛词」**，
# 它保证的是「命中更稀有的词拿更高分」。真库上这层加权对 Q1 的收益有限（top-10 里
# 含「孙正义」的条数主要由每集上限那一层决定），真正被它救回来的是 Q2（见测试）。
#
# 缓存策略（参照 _VEC_CACHE）：进程内一张 dict，键是查询词，值是「含这个词的切片数」，
# 切片总数一并记在 _DF_CACHE["total"]（_rerank 要拿它算 idf 的分母）。
# 全库统计**只在第一次用到时做一次**，之后每次搜索只是查表。
# 失效有两条独立的路，缺一不可：
#   1. 进程内写入（index_dir / embed_pending / reindex）→ 走 _vec_invalidate() 顺手清
#   2. 别的进程改了库（CLI 索引完、Web 正在跑）→ 每次搜索比一下 (切片数, last_index)，
#      对不上就重算。只看进程内标志会吃到过期数据，只比库指纹则每次搜索都要 COUNT。
_DF_CACHE: dict = {"db": "", "total": -1, "stamp": "", "df": None}


def _df_invalidate() -> None:
    """让进程内的 df（文档频率）缓存失效。"""
    _DF_CACHE.update({"db": "", "total": -1, "stamp": "", "df": None})


def _df_table(conn: sqlite3.Connection) -> dict | None:
    """全库文档频率表 {词: 含该词的切片数}；拿不到（空库/异常）返回 None。

    用 FTS5 自己的 `fts5vocab` 虚拟表来算 —— 它直接读索引，不用把 5612 条切片的分词串
    全读进 Python 再 set 一遍（真库实测：vocab 全表 11890 个词、8ms；全表取文本 + jieba
    重切一遍是 712ms，差两个数量级）。
    虚拟表建在 temp 里并立刻 drop：它是**只读视图**，不占库文件，也不会留下过期状态。
    """
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0] or 0)
    except sqlite3.Error:
        return None
    if not total:
        return None
    stamp = ""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='last_index'").fetchone()
        stamp = str(row[0]) if row else ""
    except sqlite3.Error:
        stamp = ""
    if (_DF_CACHE["df"] is not None and _DF_CACHE["db"] == str(db_path())
            and _DF_CACHE["total"] == total and _DF_CACHE["stamp"] == stamp):
        return _DF_CACHE["df"]
    name = "_pa_vocab"
    try:
        conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
        conn.execute(f"CREATE VIRTUAL TABLE temp.{name} "
                     f"USING fts5vocab('main', 'passages_fts', 'row')")
        df = {r[0]: int(r[1] or 0) for r in conn.execute(f"SELECT term, doc FROM temp.{name}")}
    except Exception:
        # 拿不到 df 不是错误，只是少一路信号：_coverage 会退回等权覆盖率。
        return None
    finally:
        try:
            conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
        except sqlite3.Error:
            pass
    if not df:
        return None
    _DF_CACHE.update({"db": str(db_path()), "total": total, "stamp": stamp, "df": df})
    return df


def _idf(tok: str, df: dict | None, total: int) -> float:
    """`idf(t) = log(1 + N / (1 + df(t)))`：越稀有越大，永不小于 log(1+N/(1+N))=0.69…"""
    if not df or total <= 0:
        return 1.0
    return math.log(1.0 + total / (1.0 + int(df.get(tok, 0))))


def embedder(model_name: str | None = None, *, allow_download: bool = True):
    """拿到嵌入模型；拿不到就返回 None（调用方退回词法检索）。

    模型来源按这个顺序试，都是本地或国内可达的：
      1. 本地缓存（fastembed 自己的 cache）
      2. HuggingFace 官方
      3. `HF_ENDPOINT` 指定的镜像（默认再试一次 https://hf-mirror.com）
    拿不到会记住原因（`embed_error()`），状态页会如实显示 —— 不装作"语义检索已启用"。
    """
    global _EMBEDDER, _EMBED_ERR
    if _EMBEDDER is not None or _EMBED_ERR:
        return _EMBEDDER
    name = model_name or EMBED_MODEL
    try:
        from fastembed import TextEmbedding
    except Exception:
        _EMBED_ERR = "没有安装 fastembed（pip install 'podcast-article[kb]' 后可用语义检索）"
        return None
    attempts = [None, os.environ.get("HF_ENDPOINT"), "https://hf-mirror.com"]
    for endpoint in attempts:
        if endpoint:
            os.environ["HF_ENDPOINT"] = endpoint
        try:
            _EMBEDDER = TextEmbedding(model_name=name, local_files_only=not allow_download)
            return _EMBEDDER
        except Exception as exc:
            _EMBED_ERR = f"{type(exc).__name__}: {str(exc)[:160]}"
    _EMBED_ERR = ("嵌入模型不可用（" + _EMBED_ERR + "）。"
                  "可设 HF_ENDPOINT 指向能用的镜像，或把 ONNX 模型放到本地后设 PA_KB_EMBED_MODEL。"
                  "不影响使用：知识库会退回纯词法检索。")
    return None


def embed_error() -> str:
    return _EMBED_ERR


def _embeddings_for(texts: list[str]) -> list[list[float]]:
    model = embedder()
    if model is None:
        return []
    return [list(map(float, v)) for v in model.embed(texts)]


def _pack(vec: list[float]) -> bytes:
    import numpy as np
    return np.asarray(vec, dtype="float32").tobytes()


def _unpack(blob: bytes, dim: int):
    import numpy as np
    return np.frombuffer(blob, dtype="float32", count=dim)


# ============================================================ 索引

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def index_dir(conn: sqlite3.Connection, ep_dir: Path, *, force: bool = False,
              embed: bool = True, log=print) -> dict:
    """索引一集的文章 + 文字稿（增量：内容没变就跳过）。"""
    ep_dir = Path(ep_dir)
    meta = {}
    mp = ep_dir / "meta.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    title = meta.get("title") or ep_dir.name
    podcast = meta.get("podcast") or ""
    url = meta.get("url") or ""
    added = 0

    for kind, fname in (("article", "article.md"), ("transcript", "transcript.txt")):
        f = ep_dir / fname
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        if len(text.strip()) < MIN_PASSAGE_CHARS:
            continue
        mtime = f.stat().st_mtime
        sha = _sha(text)
        row = conn.execute("SELECT id, sha, mtime FROM docs WHERE dir=? AND kind=?",
                           (ep_dir.name, kind)).fetchone()
        if row and not force and row["sha"] == sha:
            continue                                  # 没变，跳过（索引很便宜但没必要重算）
        if row:
            conn.execute("DELETE FROM passages_fts WHERE rowid IN "
                         "(SELECT id FROM passages WHERE doc_id=?)", (row["id"],))
            conn.execute("DELETE FROM passages WHERE doc_id=?", (row["id"],))
            conn.execute("DELETE FROM docs WHERE id=?", (row["id"],))
        cur = conn.execute(
            "INSERT INTO docs(dir, kind, title, podcast, url, mtime, sha, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (ep_dir.name, kind, title, podcast, url, mtime, sha, time.time()))
        doc_id = cur.lastrowid
        chunks = _split_article(text) if kind == "article" else _split_transcript(text)
        for i, c in enumerate(chunks):
            body = (c.get("text") or "").strip()
            if len(body) < MIN_PASSAGE_CHARS and c.get("kind") != "heading":
                continue
            toks = tokenize(f"{c.get('heading') or ''} {body}")
            if not toks:
                continue
            cur2 = conn.execute(
                "INSERT INTO passages(doc_id, ord, kind, heading, start_sec, end_sec, text) "
                "VALUES(?,?,?,?,?,?,?)",
                (doc_id, i, c.get("kind") or "body", c.get("heading") or "",
                 c.get("start_sec"), c.get("end_sec"), body))
            pid = cur2.lastrowid
            conn.execute("INSERT INTO passages_fts(rowid, tokens) VALUES(?,?)", (pid, toks))
            for name, etype, weight in extract_entities(body):
                conn.execute("INSERT OR IGNORE INTO entities(name, type, count) VALUES(?,?,0)",
                             (name, etype))
                ent = conn.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
                conn.execute("INSERT OR REPLACE INTO passage_entities(passage_id, entity_id, weight) "
                             "VALUES(?,?,?)", (pid, ent["id"], weight))
            added += 1
        conn.execute("UPDATE docs SET passages=(SELECT COUNT(*) FROM passages WHERE doc_id=?) "
                     "WHERE id=?", (doc_id, doc_id))
    conn.commit()

    if embed and added:
        pend = conn.execute(
            "SELECT p.id, p.text FROM passages p LEFT JOIN embeddings e ON e.passage_id=p.id "
            "WHERE e.passage_id IS NULL AND p.doc_id IN (SELECT id FROM docs WHERE dir=?) LIMIT 2000",
            (ep_dir.name,)).fetchall()
        if pend:
            vecs = _embeddings_for([r["text"] for r in pend])
            for row, vec in zip(pend, vecs):
                conn.execute("INSERT OR REPLACE INTO embeddings(passage_id, model, dim, vec) "
                             "VALUES(?,?,?,?)",
                             (row["id"], EMBED_MODEL, len(vec), _pack(vec)))
            conn.commit()
    # 实体计数与文档数
    conn.execute("UPDATE entities SET count=(SELECT COUNT(*) FROM passage_entities pe "
                 "WHERE pe.entity_id=entities.id)")
    conn.commit()
    _vec_invalidate()
    return {"dir": ep_dir.name, "passages": added}


def embed_pending(conn: sqlite3.Connection, *, dirs: list[str] | None = None,
                  limit: int = 20000, log=print) -> int:
    """给还没有向量的切片补上向量，返回补了几条。

    为什么要单独一个函数：index_dir 只在**新增了切片**时才嵌入，于是「先索引、后才有
    嵌入模型」的老库永远补不上向量 —— 状态页会一直显示语义检索关闭，用户也不知道
    该点哪里。重建索引并不解决它（内容没变，一切照旧跳过）。这个动作是可重复、幂等的：
    已经有向量的切片不再算第二遍。
    """
    sql = ("SELECT p.id, p.text FROM passages p LEFT JOIN embeddings e ON e.passage_id=p.id "
           "WHERE e.passage_id IS NULL")
    args: list = []
    if dirs:
        sql += f" AND p.doc_id IN (SELECT id FROM docs WHERE dir IN ({','.join('?' * len(dirs))}))"
        args += dirs
    sql += " LIMIT ?"
    args.append(limit)
    pend = conn.execute(sql, args).fetchall()
    if not pend:
        return 0
    vecs = _embeddings_for([r["text"] for r in pend])
    if not vecs:
        log(f"[kb] 还有 {len(pend)} 条切片没有向量（嵌入模型不可用：{embed_error() or '未知原因'}）")
        return 0
    done = 0
    for row, vec in zip(pend, vecs):
        if not vec:
            continue
        conn.execute("INSERT OR REPLACE INTO embeddings(passage_id, model, dim, vec) "
                     "VALUES(?,?,?,?)", (row["id"], EMBED_MODEL, len(vec), _pack(vec)))
        done += 1
    conn.commit()
    # REPLACE 会留下旧行？不会 —— passage_id 是主键，用的是同一条。但矩阵缓存必须失效。
    _vec_invalidate()
    if done:
        log(f"[kb] 补齐向量 {done} 条")
    return done


def index_all(output_root: Path, *, force: bool = False, embed: bool = True,
              progress=None, log=print) -> dict:
    """索引整个书库（只有真的变了才会写库），最后补齐缺失的向量。"""
    root = Path(output_root)
    dirs = sorted([d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")])
    conn = ensure_schema(connect())
    total = 0
    added_vecs = 0
    try:
        for i, d in enumerate(dirs):
            r = index_dir(conn, d, force=force, embed=embed, log=log)
            total += r["passages"]
            if progress:
                progress(i + 1, len(dirs), d.name)
        # 内容没变的库也要走这一步：它是「语义检索一直没打开」的唯一出路
        if embed:
            added_vecs = embed_pending(conn, log=log)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_index', ?)",
                     (str(time.time()),))
        conn.commit()
        return {"episodes": len(dirs), "passages": total, "vectors": added_vecs,
                "db": str(db_path())}
    finally:
        conn.close()


# ============================================================ 检索

def _lexical(conn: sqlite3.Connection, query: str, k: int, dirs: list[str] | None):
    """FTS5 词法检索（BM25）。中文靠 jieba 切好的 tokens 列。"""
    toks = tokenize(query)
    if not toks:
        return []
    # 用 OR 连接（BM25 自己会奖励命中更多词的结果）；短语用引号交给 FTS5
    fts_q = " OR ".join(f'"{t}"' for t in toks.split())
    sql = ("SELECT p.id, p.doc_id, p.text, p.heading, p.kind, p.start_sec, p.end_sec, "
           "  d.dir, d.title, d.podcast, d.kind AS doc_kind, bm25(passages_fts) AS score "
           "FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid "
           "JOIN docs d ON d.id = p.doc_id WHERE passages_fts MATCH ?")
    args: list = [fts_q]
    if dirs:
        sql += f" AND d.dir IN ({','.join('?' * len(dirs))})"
        args += dirs
    sql += " ORDER BY score LIMIT ?"
    args.append(k)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []


def _load_vectors(conn: sqlite3.Connection):
    """把向量读进内存矩阵（进程内缓存），个人级书库一次矩阵乘法就够。"""
    import numpy as np
    if (_VEC_CACHE["mat"] is not None and _VEC_CACHE["ids"] is not None
            and _VEC_CACHE["model"] == EMBED_MODEL
            and _VEC_CACHE["db"] == str(db_path())):        # 换库（测试/多库）必须重读
        return _VEC_CACHE
    rows = conn.execute("SELECT passage_id, dim, vec FROM embeddings WHERE model=?",
                        (EMBED_MODEL,)).fetchall()
    if not rows:
        return None
    dim = rows[0]["dim"]
    ids = np.array([r["passage_id"] for r in rows], dtype="int64")
    mat = np.vstack([_unpack(r["vec"], r["dim"]) for r in rows]).astype("float32")
    mat /= np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)
    _VEC_CACHE.update({"model": EMBED_MODEL, "ids": ids, "mat": mat, "db": str(db_path())})
    return _VEC_CACHE


def _semantic(conn: sqlite3.Connection, query: str, k: int, dirs: list[str] | None):
    import numpy as np
    cache = _load_vectors(conn)
    if not cache:
        return []
    vecs = _embeddings_for([query])
    if not vecs:
        return []
    q = np.asarray(vecs[0], dtype="float32")
    q /= max(float(np.linalg.norm(q)), 1e-9)
    sims = cache["mat"] @ q
    order = np.argsort(-sims)[: max(k * 4, k)]
    out = []
    for idx in order:
        sim = float(sims[idx])
        if sim < SEM_MIN_SIM:
            # 相似度是降序遍历的，一旦低于下限，后面的只会更低 —— 直接停。
            # 没有这条线，向量检索对**任何**输入都会返回 k 条（余弦永远有值），
            # 于是「完全无关的问题」也会拿到一堆貌似相关的原文喂给模型，比不搜更坏。
            break
        pid = int(cache["ids"][idx])
        row = conn.execute(
            "SELECT p.id, p.doc_id, p.text, p.heading, p.kind, p.start_sec, p.end_sec, "
            "       d.dir, d.title, d.podcast, d.kind AS doc_kind "
            "FROM passages p JOIN docs d ON d.id=p.doc_id WHERE p.id=?", (pid,)).fetchone()
        if not row:
            continue
        if dirs and row["dir"] not in dirs:
            continue
        d = dict(row)
        d["score"] = sim
        d["match"] = "vector"
        out.append(d)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------- 重排（RRF 之后）

# 为什么融合之后还要再加一层：**BM25 与余弦都偏爱短句**（BM25 有长度归一化，短文档的
# 词密度天然高；余弦同理），于是「一个 20 字的 ASR 碎片」能把「200 字的实质段落」挤出
# 前 6 —— 喂给模型的 6 条"资料"全是断句，再好的提示词也救不回来。
# 实测（真库，问「孙正义是怎么押注 AI 的？」）：含答案的那条 115 字正文在词法排第 14、
# 语义排第 15（余弦 0.629），而 top10 里挤满了 20~40 字的碎片，回答只能是「资料里没有」。
#
# 所以这里补一层**软重排**：RRF 分 × 覆盖率 × 长度先验 × 来源先验。
# 三样都是**乘性权重，只调顺序，不排除任何切片** —— 有些集只有文字稿没有文章，
# 那时它必须还能被选中。
LEN_FULL = (100.0, 600.0)      # 这个长度区间权重 1.0（检索粒度最好）
LEN_SHORT = 40.0               # 短于它明显降权：多半是 ASR 断句
LEN_LONG = 900.0               # 长于它轻微降权：段落太长，检索粒度差
W_SHORT, W_LONG = 0.55, 0.9

_SRC_ARTICLE = 1.0             # 文章正文 / 引用 / 要点：编辑过，信息密度最高
_SRC_HEADING = 0.7             # 文章小标题：是标签，不是内容
_SRC_TRANSCRIPT = 0.85         # 文字稿：ASR 断句、碎片多（但不排除，见上）
_SRC_OTHER = 0.9

DEDUP_JACCARD = 0.6            # 词集合重合到这个程度就算同一条信息
PER_DIR_MAX = 4                # 同一集最多给几条（凑不够 k 时会放宽）

# 融合之前每种检索各取多少条**候选**。
# 为什么不能只取 k*3：重排只能从候选里挑，而真正有信息量的长段落恰恰被长度偏置压在
# 候选池外面。实测（真库 5612 条切片）：
#   k=10（池 30）→ 含答案的 115 字正文在词法第 14、语义第 15，勉强够得着
#   k=3 （池  9）→ 它根本不在候选池里，重排再对也拿不到它
# 所以池子给一个下限：先粗捞 100 条，再由重排（覆盖率/长度/来源/去重/每集上限）挑。
# 代价可以忽略：BM25 是一条 SQL 的 LIMIT，向量那一步本来就是全表矩阵乘法。
CANDIDATE_POOL = 100


def _token_set(text: str) -> set[str]:
    """分词后的**词集合**（覆盖率与去重都用它，跟 FTS 的切法保持一致）。"""
    return set(tokenize(text).split())


def _coverage(qset: set[str], pset: set[str]) -> float:
    """查询词覆盖率（等权）：查询里的词有多少出现在这条切片里。

    这是**退回用的兜底口径**：拿不到 df 时（空库、FTS 异常）搜索不能因为少一路信号就
    排序退化或报错。正常路径用 `_coverage_idf`。
    """
    if not qset:
        return 0.0
    return len(qset & pset) / len(qset)


def _coverage_idf(qset: set[str], pset: set[str], df: dict | None,
                  total: int) -> float | None:
    """IDF 加权覆盖率：`Σ_{t∈Q∩P} idf(t) / Σ_{t∈Q} idf(t)`。

    为什么必须按信息量加权（而不是数词）：查询「孙正义是怎么押注 AI 的？」里，
    `ai` 出现在 382 条切片、`孙正义` 79 条、`押注` 8 条。等权口径下「只命中一个词」
    一律拿 0.33，于是只谈「押注」而与孙正义无关的段落（Bloomberg 那集讲 Dario Amodei）
    拿到与实质证据同档的覆盖率，混进 top-10 挤掉真正的答案。加权之后，命中稀有词
    （`押注` idf=6.44）与只命中泛词（`ai` idf=2.75）才分得开。
    取不到 df 或分母为 0 时返回 None —— 调用方退回 `_coverage`，绝不让搜索炸掉。
    """
    if not qset or not df or total <= 0:
        return None
    hit = qset & pset
    if not hit:
        return 0.0
    denom = sum(_idf(t, df, total) for t in qset)
    if denom <= 0:
        return None
    return sum(_idf(t, df, total) for t in hit) / denom


def _length_prior(n: int) -> float:
    """长度先验（软权重，不是硬过滤）：约 100–600 字最好，碎片明显降权。"""
    if n <= LEN_SHORT:
        return W_SHORT
    if n < LEN_FULL[0]:                                   # 40–100：线性过渡
        return W_SHORT + (1.0 - W_SHORT) * (n - LEN_SHORT) / (LEN_FULL[0] - LEN_SHORT)
    if n <= LEN_FULL[1]:
        return 1.0
    if n <= LEN_LONG:                                     # 600–900：线性过渡
        return 1.0 + (W_LONG - 1.0) * (n - LEN_FULL[1]) / (LEN_LONG - LEN_FULL[1])
    return W_LONG


def _source_prior(hit: dict) -> float:
    """来源先验：文章正文 > 文字稿 > 文章小标题。"""
    doc_kind = hit.get("doc_kind") or ""
    if doc_kind == "transcript":
        return _SRC_TRANSCRIPT
    if doc_kind == "article":
        return _SRC_HEADING if (hit.get("kind") or "") == "heading" else _SRC_ARTICLE
    return _SRC_OTHER


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rerank(hits, query: str, k: int, *, per_dir: int = PER_DIR_MAX,
            conn: sqlite3.Connection | None = None) -> list[dict]:
    """RRF 之后的重排：IDF 加权覆盖率 × 长度先验 × 来源先验，再去重、限每集条数。

    `rrf` 字段**原样保留**（界面上 `score` 用的就是它），新分数只写在 `rank_score` 里
    用来排序与调试。`coverage` 保留等权口径（界面/测试按它读），加权口径另写
    `coverage_idf`。
    """
    qset = _token_set(query)
    df = _df_table(conn) if conn is not None else None
    total = int(_DF_CACHE["total"]) if (df and _DF_CACHE["df"] is df) else 0
    scored: list[dict] = []
    for h in hits:
        text = re.sub(r"\s+", " ", h.get("text") or "").strip()
        pset = _token_set(f"{h.get('heading') or ''} {text}")
        cov = _coverage(qset, pset)
        cov_idf = _coverage_idf(qset, pset, df, total)
        h = dict(h)
        h["coverage"] = cov
        h["coverage_idf"] = cov if cov_idf is None else cov_idf   # 拿不到 df → 退回等权
        h["len_prior"] = _length_prior(len(text))
        h["src_prior"] = _source_prior(h)
        # 排序用加权覆盖率；`coverage` 与 `coverage_idf` 一致时（退回路径）与旧公式完全相同
        h["rank_score"] = (float(h.get("rrf") or h.get("score") or 0.0)
                           * (0.5 + 0.5 * h["coverage_idf"]) * h["len_prior"] * h["src_prior"])
        h["_tok"] = pset
        scored.append(h)
    scored.sort(key=lambda h: -h["rank_score"])

    # 去重：文本高度重合的只留一条。同一集里 6 条近义碎片等于只给模型一条信息。
    # **按集去重**（不是全局）：同一集里的重复多半只是 ASR 把一句话说了两遍、或者文章与
    # 文字稿各存了一份，丢掉一条什么也没少；而**两集说同样的话是互相印证**，是更强的
    # 证据，连出处一起丢掉反而是损失。
    kept: list[dict] = []
    seen: dict[str, list[set]] = {}
    for h in scored:
        pool = seen.setdefault(h.get("dir") or "", [])
        if any(_jaccard(h["_tok"], prev) >= DEDUP_JACCARD for prev in pool):
            continue
        pool.append(h["_tok"])
        kept.append(h)

    out = _pick_diverse(kept, k, per_dir)
    for h in out:
        h.pop("_tok", None)
    return out


# ---------------------------------------------------------------- 每集上限（自适应）

# 上限为什么不能是固定 4：用户问的是**一个人名/一个专名**时（「孙正义是怎么押注 AI 的？」
# 真库实测：含「孙正义」的 79 条切片全部来自同一集），答案所在的那一集本来就有 10 条
# 实质证据，固定上限却把它掐在 4 条，剩下的名额被别的集里只是碰巧出现某个泛词的段落填满。
# 但上限也不能直接取消：多集都有可比证据时，一集吃光全部名额会让答案失去互相印证。
#
# 判据（确定性、只看分数）：**谁最强，谁说了算**。
#   1. 先看第一名属于哪一集 D；若「别的集里最好那条」的分数 ≥ DOMINANCE_RATIO × 第一名的
#      分数 → 没有谁明显更强，D 的上限照旧 PER_DIR_MAX（来源多样性保留）。
#   2. 否则 D 就是答案所在的集，允许它一路填到 k 条。
#   3. 即便 D 占优，也**只放宽到 k**：k 条以外的切片依然进不来；别的集只要还有名额就
#      按分数补进来（真库实测：Q1 的 k=10 里 D 拿 8 条，另一集留 2 条）。
DOMINANCE_RATIO = 0.8


def _pick_diverse(kept: list[dict], k: int, per_dir: int) -> list[dict]:
    """按 rank_score 排序取 k 条，每集上限自适应（见 DOMINANCE_RATIO 的注释）。

    `kept` 必须已按 rank_score 降序。上限**永远不允许结果变少** —— 只有 1 集相关时
    必须补满 k 条（用户的书库可能就这么大）。
    """
    if not kept:
        return []
    order = sorted(range(len(kept)), key=lambda i: (-kept[i]["rank_score"], i))
    dirs = [kept[i].get("dir") or "" for i in order]
    top_dir = dirs[0]
    other_best = 0.0
    for i in order[1:]:
        if dirs[i] != top_dir:
            other_best = float(kept[i]["rank_score"])
            break
    dominant = bool(top_dir) and other_best < DOMINANCE_RATIO * float(kept[order[0]]["rank_score"])

    # 先按分数顺序给每一集「配额内的」条目（占优的那一集配额是 k，其余是 per_dir）；
    # 超出的留到第二步。为什么要留：上限的存在意义是**别让一集吃光名额**，
    # 而不是「把这一集的好证据扔掉」—— 所以别的集填不满时，它们必须还能补回来。
    taken: set[int] = set()
    over: list[int] = []
    per: dict[str, int] = {}
    for i in order:
        d = dirs[i]
        cap = k if (dominant and d == top_dir) else per_dir
        if per.get(d, 0) >= cap:
            over.append(i)
            continue
        per[d] = per.get(d, 0) + 1
        taken.add(i)
    if len(taken) < k:
        taken.update(over[: k - len(taken)])
    # **必须再按分数顺序收集一遍**：`taken` 是按配额凑出来的，直接 append 会让
    # 排在后面的集插到前面集的强证据之前（实测：8 条强证据的那一集只拿到 6 条，
    # 另外两集的弱条目反而各占 2 条）。输出给模型的顺序必须严格是分数序。
    return [kept[i] for i in order if i in taken][:k]


def search(query: str, *, k: int = 8, mode: str = "auto", dirs: list[str] | None = None,
           conn: sqlite3.Connection | None = None) -> dict:
    """混合检索：词法（FTS5/BM25）+ 向量，用 RRF 融合，再做一层软重排（见 _rerank）。

    mode: auto（有向量就混合）/ lexical / semantic。
    返回 {"mode", "hits": [...]}；每条命中都带出处（哪一集、小标题、时间戳），
    这样答案可以追溯到具体一句，而不是"我看着像"。
    """
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        has_vec = _load_vectors(conn) is not None
        use_sem = mode in ("semantic", "auto") and has_vec
        use_lex = mode != "semantic"
        # 候选池要够深：重排只能从池子里挑，见 CANDIDATE_POOL 的注释。
        pool = max(k * 3, CANDIDATE_POOL)
        lex = _lexical(conn, query, pool, dirs) if use_lex else []
        sem = _semantic(conn, query, pool, dirs) if use_sem else []

        # RRF：按名次融合，避免两种分数的量纲打架（BM25 与余弦完全不可比）
        K0 = 60.0
        fused: dict[int, dict] = {}
        for rank, hit in enumerate(lex):
            fused.setdefault(hit["id"], {**hit, "rrf": 0.0, "match": "lexical"})["rrf"] += 1 / (K0 + rank + 1)
        for rank, hit in enumerate(sem):
            e = fused.setdefault(hit["id"], {**hit, "rrf": 0.0, "match": "vector"})
            e["rrf"] += 1 / (K0 + rank + 1)
            if e.get("match") == "lexical":
                e["match"] = "both"
        # 融合后的名次只看"排在第几"，短碎片因此天然占优 —— 重排这一层就是修它。
        hits = _rerank(fused.values(), query, k, conn=conn)
        return {"mode": "hybrid" if (lex and sem) else ("semantic" if sem else "lexical"),
                "hits": hits, "count": len(hits)}
    finally:
        if own:
            conn.close()


def _hit_public(hit: dict, *, limit: int = 320) -> dict:
    text = re.sub(r"\s+", " ", hit.get("text") or "").strip()
    return {
        "dir": hit.get("dir"), "title": hit.get("title"), "podcast": hit.get("podcast"),
        "doc_kind": hit.get("doc_kind"), "kind": hit.get("kind"), "heading": hit.get("heading"),
        "start_sec": hit.get("start_sec"), "end_sec": hit.get("end_sec"),
        "text": text[:limit], "match": hit.get("match"),
        # score 仍然是 RRF 分（界面语义不变）；重排分单独给一个字段，只为调试。
        "score": round(float(hit.get("rrf") or hit.get("score") or 0), 5),
        "rank_score": round(float(hit.get("rank_score") or 0), 6),
    }


# ============================================================ 实体

def entities(*, limit: int = 60, etype: str | None = None, min_count: int = 2,
             generic_ratio: float = 0.85, conn: sqlite3.Connection | None = None) -> list[dict]:
    """实体榜。默认把「几乎每集都出现」的泛词滤掉（美国/公司/市场这种），
    它们排在榜首只会把真正有信息量的人物、机构挤下去。"""
    total_docs = None if conn else None
    return _entities_impl(limit=limit, etype=etype, min_count=min_count,
                          generic_ratio=generic_ratio, conn=conn)


def _entities_impl(*, limit: int, etype: str | None, min_count: int,
                   generic_ratio: float, conn: sqlite3.Connection | None):
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        doc_total = conn.execute(
            "SELECT COUNT(DISTINCT dir) FROM docs").fetchone()[0] or 1
        sql = ("SELECT e.id, e.name, e.type, e.count, "
               "       COUNT(DISTINCT d.dir) AS docs "
               "FROM entities e JOIN passage_entities pe ON pe.entity_id=e.id "
               "JOIN passages p ON p.id=pe.passage_id JOIN docs d ON d.id=p.doc_id "
               "WHERE e.count>=?")
        args: list = [min_count]
        if etype:
            sql += " AND e.type=?"
            args.append(etype)
        sql += " GROUP BY e.id HAVING docs <= ? ORDER BY e.count DESC, e.name LIMIT ?"
        args += [max(1, int(doc_total * generic_ratio)), limit]
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        if own:
            conn.close()


def entity_detail(name: str, *, limit: int = 40, conn: sqlite3.Connection | None = None) -> dict:
    """某个实体的全部出处（按集分组，带时间戳）。"""
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        row = conn.execute("SELECT * FROM entities WHERE name=?", (name,)).fetchone()
        if not row:
            return {"name": name, "found": False, "passages": []}
        rows = conn.execute(
            "SELECT p.text, p.heading, p.kind, p.start_sec, p.end_sec, d.dir, d.title, "
            "       d.podcast, d.kind AS doc_kind, pe.weight "
            "FROM passage_entities pe JOIN passages p ON p.id=pe.passage_id "
            "JOIN docs d ON d.id=p.doc_id WHERE pe.entity_id=? "
            "ORDER BY pe.weight DESC, p.start_sec LIMIT ?", (row["id"], limit)).fetchall()
        return {"name": row["name"], "type": row["type"], "count": row["count"],
                "found": True, "passages": [_hit_public(dict(r)) for r in rows]}
    finally:
        if own:
            conn.close()


# ============================================================ 记忆

def _migrate_memory_columns(conn: sqlite3.Connection) -> None:
    """给已存在的 memory 表补新列（**只 ALTER，绝不重建**）。

    `CREATE TABLE IF NOT EXISTS` 不会给老表加列，而 memory 又是唯一不能重建的表：
    所以新增字段只能靠 ALTER 一个个补。缺列时读出来会直接报 no such column，
    等于「升级一次，记忆功能全挂」，所以这步必须在 ensure_schema 里无条件跑。
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(memory)").fetchall()}
    if not have:
        return                                   # 表刚建出来，_SCHEMA 已经带全了
    for col, ddl in (("use_count", "use_count INTEGER DEFAULT 0"),
                     ("last_used_at", "last_used_at REAL DEFAULT 0")):
        if col not in have:
            conn.execute(f"ALTER TABLE memory ADD COLUMN {ddl}")


def memory_add(text: str, *, kind: str = "fact", tags: str = "", source_dir: str = "",
               source_kind: str = "user", weight: float = 1.0, pinned: bool = False,
               conn: sqlite3.Connection | None = None) -> dict:
    """写一条记忆。kind 见 MEMORY_KINDS；pinned 的记忆永远参与检索。"""
    kind = kind if kind in MEMORY_KINDS else "fact"
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) < 2:
        raise ValueError("记忆内容太短")
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        now = time.time()
        cur = conn.execute(
            "INSERT INTO memory(kind, text, tags, source_dir, source_kind, weight, pinned, "
            "                   created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, text, tags, source_dir, source_kind, weight, 1 if pinned else 0, now, now))
        mid = cur.lastrowid
        conn.execute("INSERT INTO memory_fts(rowid, tokens) VALUES(?,?)", (mid, tokenize(text)))
        conn.commit()
        return memory_get(mid, conn=conn)
    finally:
        if own:
            conn.close()


def memory_get(mid: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        row = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def memory_list(*, kind: str | None = None, query: str = "", limit: int = 200,
                conn: sqlite3.Connection | None = None) -> list[dict]:
    """列记忆：置顶优先，然后新的在前；给了 query 就走 FTS5。"""
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        if query.strip():
            toks = tokenize(query)
            if toks:
                fts_q = " OR ".join(f'"{t}"' for t in toks.split())
                rows = conn.execute(
                    "SELECT m.* FROM memory_fts f JOIN memory m ON m.id=f.rowid "
                    "WHERE memory_fts MATCH ? ORDER BY m.pinned DESC, rank LIMIT ?",
                    (fts_q, limit)).fetchall()
                if not kind:
                    return [dict(r) for r in rows]
                return [dict(r) for r in rows if r["kind"] == kind]
        sql = "SELECT * FROM memory"
        args: list = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        if own:
            conn.close()


def memory_update(mid: int, **fields) -> dict | None:
    """改记忆（文本 / 类型 / 置顶）。改文本要同步重建它的 FTS 行。"""
    conn = ensure_schema(connect())
    try:
        row = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        text = re.sub(r"\s+", " ", str(fields.get("text", row["text"]))).strip()
        kind = fields.get("kind", row["kind"])
        kind = kind if kind in MEMORY_KINDS else row["kind"]
        pinned = int(bool(fields.get("pinned", row["pinned"])))
        tags = fields.get("tags", row["tags"])
        conn.execute("UPDATE memory SET text=?, kind=?, tags=?, pinned=?, updated_at=? WHERE id=?",
                     (text, kind, tags, pinned, time.time(), mid))
        conn.execute("DELETE FROM memory_fts WHERE rowid=?", (mid,))
        conn.execute("INSERT INTO memory_fts(rowid, tokens) VALUES(?,?)", (mid, tokenize(text)))
        conn.commit()
        return memory_get(mid, conn=conn)
    finally:
        conn.close()


def memory_delete(mid: int) -> bool:
    """删一条记忆。返回**是否真的删掉了**。

    不能用 `conn.total_changes`：那个计数是整条连接累计的，而 `ensure_schema` 每次都会
    往 meta 写一条（于是它永远 > 0）—— 结果是「删一个不存在的 id 也回 true」，
    上层与界面都会以为删成功了，这是最容易被忽略的一种谎报。用 DELETE 的 rowcount。
    """
    conn = ensure_schema(connect())
    try:
        cur = conn.execute("DELETE FROM memory WHERE id=?", (mid,))
        deleted = cur.rowcount > 0
        conn.execute("DELETE FROM memory_fts WHERE rowid=?", (mid,))
        conn.commit()
        return deleted
    finally:
        conn.close()


def memory_for_prompt(query: str = "", *, limit: int = 6,
                      conn: sqlite3.Connection | None = None) -> list[dict]:
    """挑出该塞进 prompt 的记忆：置顶的 + 与当前问题相关的，去重。

    顺手记下**谁真的被用了**（use_count / last_used_at）。这不是统计癖：记忆腐化最常见
    的样子就是「存了一堆，没人看」，而用户只有看到「这条 0 次」才知道该删哪条。

    只统计**真的对上号**的：置顶的（用户明确要求永远带上）+ 与当前问题相关的。
    查询没命中时用来垫底的那几条**不算** —— 否则任何一条记忆只要被顺手塞进 prompt
    就永远是「用过」，这个信号立刻变成废数。
    """
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        pinned = [dict(r) for r in conn.execute(
            "SELECT * FROM memory WHERE pinned=1 ORDER BY updated_at DESC LIMIT ?", (limit,))]
        seen = {m["id"] for m in pinned}
        matched = [m for m in memory_list(query=query, limit=limit * 2, conn=conn)
                   if m["id"] not in seen] if query.strip() else []
        rest = matched or [m for m in memory_list(limit=limit, conn=conn)
                           if m["id"] not in seen]
        picked = (pinned + rest)[:limit]
        counted = {m["id"] for m in pinned} | {m["id"] for m in matched}
        ids = [m["id"] for m in picked if m["id"] in counted]
        if ids:
            now = time.time()
            conn.executemany("UPDATE memory SET use_count=use_count+1, last_used_at=? WHERE id=?",
                             [(now, mid) for mid in ids])
            conn.commit()
        return picked
    finally:
        if own:
            conn.close()


def memory_never_used(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    """一条都没被用过的记忆（按写入时间倒序）——「该不该留着」的复查清单。"""
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memory WHERE use_count=0 ORDER BY created_at DESC").fetchall()]
    finally:
        if own:
            conn.close()


# ============================================================ 状态

def stats(conn: sqlite3.Connection | None = None, *,
          output_root: Path | None = None) -> dict:
    """书库与记忆的规模。output_root 由调用方给（Web 与 CLI 的输出目录可以不同）。"""
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        docs = q("SELECT COUNT(*) FROM docs")
        passages = q("SELECT COUNT(*) FROM passages")
        vectors = q("SELECT COUNT(*) FROM embeddings")
        ents = q("SELECT COUNT(*) FROM entities")
        mem = q("SELECT COUNT(*) FROM memory")
        last = conn.execute("SELECT value FROM meta WHERE key='last_index'").fetchone()
        try:
            size = db_path().stat().st_size
        except OSError:
            size = 0
        # 磁盘上有几集：这才是用户眼里的「书库有多大」。只报索引过的集数会误导 ——
        # 新生成、还没入库的文章会显得"凭空消失"（CLI 的 status 就踩过这个：显示 0 集）。
        root = Path(output_root) if output_root else (PROJECT_ROOT / "output")
        try:
            on_disk = len([d for d in root.iterdir()
                           if d.is_dir() and not d.name.startswith(".")])
        except OSError:
            on_disk = 0
        return {
            "db": str(db_path()), "size_bytes": size,
            "docs": docs, "passages": passages, "vectors": vectors,
            "entities": ents, "memory": mem,
            "episodes_on_disk": on_disk,
            "episodes_indexed": q("SELECT COUNT(DISTINCT dir) FROM docs"),
            "model": EMBED_MODEL,
            "semantic": vectors > 0,
            "embed_ready": embedder(allow_download=False) is not None or vectors > 0,
            "embed_error": embed_error(),
            "last_index": float(last["value"]) if last else None,
            "schema": SCHEMA_VERSION,
        }
    finally:
        if own:
            conn.close()


def reindex(output_root: Path, *, force: bool = True, embed: bool = True, progress=None) -> dict:
    """重建（派生数据，删了再来最省事）。"""
    conn = ensure_schema(connect())
    try:
        for t in ("passage_entities", "passages_fts", "embeddings", "passages", "entities", "docs"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()
    return index_all(output_root, force=force, embed=embed, progress=progress)
