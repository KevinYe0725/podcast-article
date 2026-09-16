// 造一份测试数据（默认落在 PA_OUTPUT_DIR，未设置则用仓库 output/）
const fs = require("fs");
const path = require("path");

const out = process.env.PA_OUTPUT_DIR
  || path.join(__dirname, "..", "..", "output");
const dir = path.join(out, "__UI测试单集");
fs.rmSync(dir, { recursive: true, force: true });
fs.mkdirSync(dir, { recursive: true });

fs.writeFileSync(path.join(dir, "meta.json"), JSON.stringify({
  title: "UI 测试单集", podcast: "测试台", author: "某人",
  url: "https://example.com/ui", pub_date: "2024-01-01T00:00:00Z", duration: 3661,
}));
fs.writeFileSync(path.join(dir, "article.md"),
  "# 他把名字缝在鱼身上\n\n> 一句话引语，只说张力不交底。\n\n" +
  "## 第一节：他用一根针把名字缝在鱼身上\n\n" +
  "1906 年 4 月 18 日清晨，旧金山发生里氏 7.9 级地震。他把标签缝进鱼喉咙。\n\n" +
  "> 这是原话 [00:10:07]\n\n" +
  "## 读完你会带走什么\n\n- 一条可检验的判断\n- 一条可试的做法\n");
fs.writeFileSync(path.join(dir, "transcript.txt"), "[00:00:01] 开头\n[00:10:07] 这是原话\n");
fs.writeFileSync(path.join(dir, "transcript.json"), "[]");
fs.writeFileSync(path.join(dir, "audio.m4a"), Buffer.alloc(64 * 1024, 0));
// 1x1 的合法 PNG（做卡片封面用；测试要验证 /api/cover 能被卡片引用到）
fs.writeFileSync(path.join(dir, "cover.png"), Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64"));
console.log("seeded:", dir);
