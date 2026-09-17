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
import os
import re
import sqlite3
import time
from pathlib import Path

from .config import PROJECT_ROOT

SCHEMA_VERSION = 2
DB_PATH = Path(os.environ.get("PA_KB_FILE") or (PROJECT_ROOT / "kb.sqlite"))
EMBED_MODEL = os.environ.get("PA_KB_EMBED_MODEL") or "BAAI/bge-small-zh-v1.5"
PASSAGE_CHARS = 420          # 单条切片的目标长度（检索粒度：太小丢上下文，太大降精度）
MIN_PASSAGE_CHARS = 30

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
    updated_at REAL DEFAULT 0
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
    """建表；schema 版本变了就重建（知识库是派生数据，重建比迁移便宜）。"""
    have = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    version = None
    if have:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = row["value"] if row else None
    if version is not None and int(version) != SCHEMA_VERSION:
        for t in ("passage_entities", "passages_fts", "memory_fts", "embeddings",
                  "passages", "entities", "memory", "docs", "meta"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


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
_VEC_CACHE: dict = {"model": "", "ids": None, "mat": None}


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
    _VEC_CACHE["ids"] = None
    return {"dir": ep_dir.name, "passages": added}


def index_all(output_root: Path, *, force: bool = False, embed: bool = True,
              progress=None, log=print) -> dict:
    """索引整个书库（只有真的变了才会写库）。"""
    root = Path(output_root)
    dirs = sorted([d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")])
    conn = ensure_schema(connect())
    total = 0
    try:
        for i, d in enumerate(dirs):
            r = index_dir(conn, d, force=force, embed=embed, log=log)
            total += r["passages"]
            if progress:
                progress(i + 1, len(dirs), d.name)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_index', ?)",
                     (str(time.time()),))
        conn.commit()
        return {"episodes": len(dirs), "passages": total, "db": str(db_path())}
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
    if _VEC_CACHE["mat"] is not None and _VEC_CACHE["model"] == EMBED_MODEL:
        return _VEC_CACHE
    rows = conn.execute("SELECT passage_id, dim, vec FROM embeddings WHERE model=?",
                        (EMBED_MODEL,)).fetchall()
    if not rows:
        return None
    dim = rows[0]["dim"]
    ids = np.array([r["passage_id"] for r in rows], dtype="int64")
    mat = np.vstack([_unpack(r["vec"], r["dim"]) for r in rows]).astype("float32")
    mat /= np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)
    _VEC_CACHE.update({"model": EMBED_MODEL, "ids": ids, "mat": mat})
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
        d["score"] = float(sims[idx])
        d["match"] = "vector"
        out.append(d)
        if len(out) >= k:
            break
    return out


def search(query: str, *, k: int = 8, mode: str = "auto", dirs: list[str] | None = None,
           conn: sqlite3.Connection | None = None) -> dict:
    """混合检索：词法（FTS5/BM25）+ 向量，用 RRF 融合。

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
        lex = _lexical(conn, query, k * 3, dirs) if use_lex else []
        sem = _semantic(conn, query, k * 3, dirs) if use_sem else []

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
        hits = sorted(fused.values(), key=lambda h: -h["rrf"])[:k]
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
        "score": round(float(hit.get("rrf") or hit.get("score") or 0), 5),
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
    conn = ensure_schema(connect())
    try:
        conn.execute("DELETE FROM memory WHERE id=?", (mid,))
        conn.execute("DELETE FROM memory_fts WHERE rowid=?", (mid,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def memory_for_prompt(query: str = "", *, limit: int = 6,
                      conn: sqlite3.Connection | None = None) -> list[dict]:
    """挑出该塞进 prompt 的记忆：置顶的 + 与当前问题相关的，去重。"""
    own = conn is None
    conn = conn or ensure_schema(connect())
    try:
        pinned = [dict(r) for r in conn.execute(
            "SELECT * FROM memory WHERE pinned=1 ORDER BY updated_at DESC LIMIT ?", (limit,))]
        seen = {m["id"] for m in pinned}
        rest = [m for m in memory_list(query=query, limit=limit * 2, conn=conn)
                if m["id"] not in seen] if query.strip() else []
        if not rest:
            rest = [m for m in memory_list(limit=limit, conn=conn) if m["id"] not in seen]
        return (pinned + rest)[:limit]
    finally:
        if own:
            conn.close()


# ============================================================ 状态

def stats(conn: sqlite3.Connection | None = None) -> dict:
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
        return {
            "db": str(db_path()), "size_bytes": size,
            "docs": docs, "passages": passages, "vectors": vectors,
            "entities": ents, "memory": mem,
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
