"""程序化生成一版**备选**宣传海报 → docs/alt-banner.png。

注意：`docs/banner.png` 现在是**设计好的成品图**（不是这个脚本画的），README 直接引用它。
所以这个脚本**故意不再写 banner.png** —— 否则谁跑一次就会把成品图覆盖掉且找不回来。
要换成品图请直接替换文件。

用法：uv run python scripts/make_banner.py
"""
from PIL import Image, ImageDraw, ImageFont
import math, random

W, H = 2560, 1280
GREEN, GREEN2 = (16, 163, 127), (94, 232, 192)
TXT, TXT_DIM = (242, 247, 245), (143, 166, 160)
LINE = (42, 58, 68)

img = Image.new("RGB", (W, H), (10, 20, 28))
d = ImageDraw.Draw(img)

# ---- 背景垂直渐变 #0B141E -> #0C1A17
top, bot = (11, 20, 30), (12, 26, 23)
for y in range(H):
    t = y / H
    d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))

# ---- 柔光：右上绿色 radial + 左下暗青
glow = Image.new("L", (W, H), 0)
gd = ImageDraw.Draw(glow)
cx, cy, R = int(W * 0.82), int(H * 0.16), 900
for r in range(R, 0, -8):
    gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=int(52 * (1 - r / R) ** 2))
glow_c = Image.new("RGB", (W, H), GREEN)
img = Image.composite(glow_c, img, glow)
d = ImageDraw.Draw(img)
glow2 = Image.new("L", (W, H), 0)
gd2 = ImageDraw.Draw(glow2)
cx, cy, R = int(W * 0.06), int(H * 0.95), 700
for r in range(R, 0, -8):
    gd2.ellipse([cx - r, cy - r, cx + r, cy + r], fill=int(36 * (1 - r / R) ** 2))
img = Image.composite(Image.new("RGB", (W, H), (30, 90, 80)), img, glow2)
d = ImageDraw.Draw(img)

# ---- 字体
def sf(size, weight=400):
    f = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size)
    f.set_variation_by_axes([float(weight)])
    return f

def cn(size, bold=False):
    return ImageFont.truetype("/System/Library/Fonts/Hiragino Sans GB.ttc", size, index=2 if bold else 0)

M = 200  # 左边距

# ---- 顶部 chip
chip_f = sf(40, 560)
chip_txt = "PODCAST  ·  VIDEO  →  ARTICLE"
tw = d.textlength(chip_txt, font=chip_f)
d.rounded_rectangle([M, 150, M + tw + 76, 150 + 84], radius=42, outline=(46, 74, 66), width=3)
d.ellipse([M + 40, 150 + 42 - 9, M + 40 + 18, 150 + 42 + 9], fill=GREEN2)
d.text((M + 84, 150 + 42), chip_txt, font=chip_f, fill=(126, 226, 181), anchor="lm")

# ---- 主标题
f1 = sf(196, 720)
d.text((M - 8, 300), "podcast", font=f1, fill=TXT, anchor="lm")
w1 = d.textlength("podcast", font=f1)
d.text((M - 8 + w1, 300), "-article", font=f1, fill=(45, 217, 160), anchor="lm")

# ---- 中文标语
d.text((M - 4, 500), "把播客，变成值得收藏的文章。", font=cn(84, True), fill=TXT, anchor="lm")

# ---- 英文副标
d.text((M - 2, 634), "Turn any podcast or video link into an article worth keeping.",
       font=sf(52, 400), fill=TXT_DIM, anchor="lm")

# ---- 特性 chips
feats = ["本地转写", "DeepSeek 精读", "实时进度", "MCP 接入", "一键写入 Notion"]
fx = M
fy = 760
for t in feats:
    ff = cn(42)
    tw = d.textlength(t, font=ff)
    d.rounded_rectangle([fx, fy, fx + tw + 64, fy + 78], radius=39, outline=(38, 56, 52), width=3)
    d.text((fx + 32, fy + 39), t, font=ff, fill=(169, 196, 188), anchor="lm")
    fx += tw + 64 + 26

# ---- 波形主视觉
random.seed(42)
bx, by, bw, bgap = M, 1060, 11, 9
n = int((W - 2 * M) / (bw + bgap))
play_x = bx + n * 0.62 * (bw + bgap)
overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
od = ImageDraw.Draw(overlay)
for i in range(n):
    x = bx + i * (bw + bgap)
    e = (0.42 + 0.30 * math.sin(i * 0.21 + 1.7) + 0.24 * math.sin(i * 0.052 + 0.5)
         + 0.16 * random.random())
    e = max(0.06, min(1.0, e))
    hmax = 155 * e
    t = i / n
    col = tuple(int(GREEN[k] + (GREEN2[k] - GREEN[k]) * t) for k in range(3))
    alpha = int(120 + 135 * (0.5 + 0.5 * math.sin(i * 0.31 + 2.2)))
    od.rounded_rectangle([x, by - hmax, x + bw, by + hmax], radius=bw / 2, fill=col + (alpha,))
img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
d = ImageDraw.Draw(img)

# 播放头
d.line([(play_x, by - 180), (play_x, by + 160)], fill=(220, 245, 236), width=4)
d.ellipse([play_x - 12, by - 12, play_x + 12, by + 12], fill=TXT)
d.text((play_x + 26, by - 180), "01:49:24", font=sf(40, 560), fill=TXT, anchor="lm")

# ---- 底部信息
d.text((M, 1240), "github.com/KevinYe0725/podcast-article", font=sf(38, 480), fill=(90, 115, 108), anchor="lm")
src = "xiaoyuzhoufm · YouTube · Bilibili · Apple Podcasts · RSS"
sw = d.textlength(src, font=sf(38, 480))
d.text((W - M - sw, 1240), src, font=sf(38, 480), fill=(90, 115, 108), anchor="lm")

# 故意不写 docs/banner.png：那是设计好的成品图，被覆盖就找不回来了。
img.save("docs/alt-banner.png", optimize=True)
img.resize((1280, 640), Image.LANCZOS).save("docs/alt-social-preview.png", optimize=True)
print("done:", W, "x", H, "->", "docs/alt-banner.png + docs/alt-social-preview.png")
print("提示：README 用的是 docs/banner.png（成品图），本脚本不会覆盖它。")
