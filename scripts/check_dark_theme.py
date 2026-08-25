"""Fail when frontend CSS grows a light-only color with no dark-theme counterpart.

index.css 里绝大多数颜色走 :root 令牌，切皮肤自动跟着翻面；但仍有近千处硬编码色值。
本脚本把「亮色专属」的色值挑出来（浅色底、浅色描边、深色文字），逐条确认在
`:root[data-theme="dark"]` 覆盖层里有同选择器同属性的对应声明，缺一条就报一条。

Usage:
    py scripts/check_dark_theme.py            # 校验，缺覆盖时退出码 1
    py scripts/check_dark_theme.py --emit     # 打印缺失项的建议 CSS（人工校对后再贴）
"""
from __future__ import annotations

import argparse
import colorsys
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "frontend" / "src" / "index.css"
PAGE_CSS_DIR = ROOT / "frontend" / "src" / "styles"


def stylesheets() -> list[tuple[str, str]]:
    """(名字, 内容) —— 页面样式表随该页懒加载分包一起下载，检查时要按
    「index.css + 这一页」的真实组合来看：页面里的亮色专属色值可以由本页的
    暗色覆盖兜住，也可以由 index.css 兜住，但不能指望别的页面。"""
    base = CSS.read_text(encoding="utf-8")
    out = [("index.css", base)]
    if PAGE_CSS_DIR.is_dir():
        for path in sorted(PAGE_CSS_DIR.glob("*.css")):
            out.append((f"styles/{path.name}", base + "\n" + path.read_text(encoding="utf-8")))
    return out

DARK_ROOT = ':root[data-theme="dark"]'
# 覆盖层的选择器一律裹 :where()：它不贡献优先级，于是暗色规则与被它覆盖的亮色规则
# 优先级完全相同，靠「写在后面」取胜。不这么写的话 `:root[data-theme=dark] .foo`
# 会比 `.foo:hover` 还高一级，夜里所有 hover / focus 反馈都会失灵。
DARK_SCOPE = f":where({DARK_ROOT})"

# 这些区域在亮色下本来就是深色面（侧栏、深色弹层、播放器底、灯箱、定妆照轨道），
# 两套皮肤共用一份样式，不需要也不应该有暗色覆盖。
DARK_PANEL_HINTS = (
    ".spine",
    ".workspace-switcher",
    ".workspace-avatar",
    ".workspace-create",
    ".user-menu",
    ".theme-switch",  # 挂在深色用户菜单里
    # 登录 / 首次改密的左侧品牌栏：两套皮肤共用同一份深色视觉
    ".auth-aside",
    ".auth-brand",
    ".auth-seal",
    ".auth-pitch",
    ".auth-flow",
    ".toast",
    ".video-preview",
    ".video-playback",
    ".ab-",
    ".cinema-preview",
    ".portrait-",
    ".character-portrait",
    ".lightbox",
    ".scene-candidate-image",
    ".scene-view-card img",
    ".scene-image-button",
    ".login-seal",
    ".brand-copy",
    ".frame-card video",
    ".rev-video",
    ".material-video-input",
    ".slide-right",
    ".qa-diff",
    ".volume-cover",
    ".mask-hint",
)

# 名字撞上了上面的前缀，但其实是浅色面，别跟着豁免。
DARK_PANEL_EXCEPTIONS = (
    ".spine-card",  # 分镜编辑器里的卡片，不是左侧栏
    ".portrait-candidate-item",  # 候选列表项，压在浅色面板上
)


def is_dark_panel(selector: str) -> bool:
    """整段选择器要逐个逗号分支判断：`.character-portrait, .scene-visual`
    里只有前者是深色面，后者仍然需要暗色对应值。"""
    if any(exc in selector for exc in DARK_PANEL_EXCEPTIONS):
        return False
    return any(hint in selector for hint in DARK_PANEL_HINTS)

COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")
VAR_FALLBACK_RE = re.compile(r"var\(\s*--[a-z0-9-]+\s*,[^)]*\)")
TEXT_PROPS = ("color", "fill", "stroke", "-webkit-text-fill-color", "caret-color")


def _light_neutral(rgb: tuple[int, int, int]) -> bool:
    """浅色中性面：宣纸白、米色、浅灰都算；带明确色相的强调色不算。"""
    return _chroma(rgb) <= 40 and _luminance(rgb) >= 0.72


def _hex_rgb(value: str) -> tuple[int, int, int] | None:
    raw = value.lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) < 6:
        return None
    try:
        return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def _rgba_parts(value: str) -> tuple[tuple[int, int, int], float] | None:
    if "(" not in value or ")" not in value:
        return None
    body = value[value.index("(") + 1 : value.rindex(")")]
    parts = [p.strip() for p in body.replace("/", ",").split(",") if p.strip()]
    if len(parts) < 3:
        return None
    try:
        rgb = tuple(int(float(p)) for p in parts[:3])
        alpha = float(parts[3]) if len(parts) > 3 else 1.0
    except ValueError:
        return None
    return rgb, alpha  # type: ignore[return-value]


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def _chroma(rgb: tuple[int, int, int]) -> int:
    return max(rgb) - min(rgb)


@dataclass(frozen=True)
class Occurrence:
    line: int
    at_rule: str
    selector: str
    prop: str
    decl: str
    color: str


def parse(css: str) -> list[Occurrence]:
    """按 { } ; 扫描，不按行——这份 CSS 里渐变和 transition 常常跨好几行。

    嵌套只有 @media/@supports 包一层，没有 CSS 嵌套语法，所以一个选择器栈够用。
    """
    out: list[Occurrence] = []
    stack: list[str] = []
    at_stack: list[str] = []
    buf = ""
    buf_line = 1
    line = 1
    depth = 0  # 括号深度：url(data:...;base64) 里的分号不算声明结束
    i = 0
    n = len(css)
    while i < n:
        ch = css[i]
        if ch == "/" and css[i + 1 : i + 2] == "*":
            end = css.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += css.count("\n", i, end)
            i = end
            continue
        if ch == "\n":
            line += 1
            i += 1
            if buf.strip():
                buf += " "
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and ch in "{};":
            text = buf.strip()
            buf = ""
            if ch == "{":
                if text.startswith("@"):
                    at_stack.append(text)
                    stack.append("")
                else:
                    stack.append(text)
            elif ch == "}":
                if stack:
                    closed = stack.pop()
                    if closed == "" and at_stack:
                        at_stack.pop()
            if ch == ";" and text and ":" in text and stack:
                _collect(out, text, stack, at_stack, buf_line)
            i += 1
            continue
        if not buf.strip() and not ch.isspace():
            buf_line = line
        buf += ch
        i += 1
    return out


def _collect(
    out: list[Occurrence],
    text: str,
    stack: list[str],
    at_stack: list[str],
    lineno: int,
) -> None:
    prop = text.split(":", 1)[0].strip()
    if " " in prop or "," in prop:  # 不是声明（多半是选择器残片）
        return
    selector = " ".join(s for s in stack if s)
    scannable = VAR_FALLBACK_RE.sub("var(--x)", text)
    for color in COLOR_RE.findall(scannable) or [""]:
        out.append(
            Occurrence(
                line=lineno,
                at_rule=at_stack[-1] if at_stack else "",
                selector=selector,
                prop=prop,
                decl=re.sub(r"\s+", " ", text).strip(),
                color=color,
            )
        )


def normalize_selector(sel: str) -> str:
    """跨行写的 :where( a, b ) 在源文件和覆盖层里空白不一样，比对前先抹平。"""
    return re.sub(r"\s+", " ", sel).replace("( ", "(").replace(" )", ")").strip()


def split_selectors(selector: str) -> list[str]:
    """按逗号拆选择器，但要躲开 :is(a, b) / :not(a, b) 括号里的逗号。"""
    out: list[str] = []
    depth = 0
    current = ""
    for ch in selector:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            if current.strip():
                out.append(normalize_selector(current))
            current = ""
            continue
        current += ch
    if current.strip():
        out.append(normalize_selector(current))
    return out


def needs_dark_counterpart(occ: Occurrence) -> bool:
    """只挑「暗色下必然出错」的色值：浅色面、浅色描边、深色文字。"""
    if not occ.color or occ.prop.startswith("--") or not occ.selector:
        return False
    if occ.selector.startswith((DARK_SCOPE, DARK_ROOT)) or occ.selector in {":root", "html", ":root, html"}:
        return False
    if all(is_dark_panel(sel) for sel in split_selectors(occ.selector)):
        return False

    color = occ.color.lower().replace(" ", "")
    is_text = occ.prop in TEXT_PROPS
    is_border = occ.prop.startswith("border") or occ.prop.startswith("outline")
    is_surface = occ.prop.startswith("background")

    if color.startswith("#"):
        rgb = _hex_rgb(color)
        if rgb is None:
            return False
        lum = _luminance(rgb)
        if is_text:
            return lum <= 0.62
        if is_border:
            return lum >= 0.62
        if is_surface:
            return lum >= 0.70
        return False

    parsed = _rgba_parts(color)
    if parsed is None:
        return False
    rgb, alpha = parsed
    light_neutral = _light_neutral(rgb)
    dark_wash = max(rgb) <= 90 and _chroma(rgb) <= 30
    if is_text:
        return dark_wash and alpha >= 0.3
    if is_border:
        return (light_neutral and alpha >= 0.25) or (dark_wash and alpha <= 0.35)
    if is_surface:
        # 0.22 起就已经是一块看得出来的面了（再低就只是层高光，翻不翻都行）
        return (light_neutral and alpha >= 0.22) or (dark_wash and alpha <= 0.2)
    return False


def dark_value(occ: Occurrence) -> str:
    """把一整条声明翻成暗色版本（同一条里可能有多个色值，逐个换）。"""
    decl = occ.decl
    for color in COLOR_RE.findall(decl):
        swapped = _dark_color(occ, color)
        if swapped:
            decl = decl.replace(color, swapped)
    return decl


def _dark_color(occ: Occurrence, color: str) -> str | None:
    raw = color.lower().replace(" ", "")
    is_text = occ.prop in TEXT_PROPS
    is_border = occ.prop.startswith("border") or occ.prop.startswith("outline")

    if raw.startswith("#"):
        rgb = _hex_rgb(raw)
        if rgb is None:
            return None
        lum = _luminance(rgb)
        hue, light, sat = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
        chroma = _chroma(rgb)
        if is_text:
            if lum > 0.62:
                return None  # 深色面上的浅色文字，两套皮肤通用
            if chroma < 24:  # 中性文字直接落回令牌，别再造一套灰
                if lum <= 0.30:
                    return "var(--ink)"
                if lum <= 0.46:
                    return "var(--ink-soft)"
                return "var(--ink-faint)"
            return _hsl_hex(hue, 0.62, 0.74)
        if is_border:
            if lum < 0.62:
                return None
            if chroma < 24:
                return "var(--hairline)" if lum >= 0.80 else "var(--hairline-strong)"
            return _tint(hue, 0.36)
        if lum < 0.70:
            return None
        if chroma < 12:
            # 中性的「宣纸白」直接落回面令牌，别在暗色里另造一套灰
            if lum >= 0.985:
                return "var(--card)"
            if lum >= 0.955:
                return "var(--surface-sunken)"
            if lum >= 0.90:
                return "var(--surface-muted)"
            return "var(--surface-strong)"
        # 有色相的状态底色（告警黄、成功绿、朱砂红……）：不要各算各的暗色，
        # 那样会得到一堆发闷的近黑。统一成「同色相的亮色 + 低透明度」水洗在面上，
        # 底色仍看得出是红/绿/黄，而且和相邻卡片是同一个家族。
        return _tint(hue, 0.18)

    parsed = _rgba_parts(raw)
    if parsed is None:
        return None
    rgb, alpha = parsed
    if _light_neutral(rgb):
        # 半透明的浅色描边翻过来该是「比面更亮的一条线」，直接落回 --hairline，
        # 用 card-rgb 会得到一条和面同色、等于看不见的边。
        if is_border:
            return "var(--hairline)"
        if alpha >= 0.8:
            # 这一档是要挡住底下内容的实心面（吸顶条、浮层、输入框），保持原样的不透明度
            return f"rgba(var(--card-rgb), {_fmt(alpha)})"
        # 这一档在亮色里是「把底面往白里提一点」的水洗（分区卡片、表头、分页签条）。
        # 翻到暗色如果照搬 card-rgb，就会和身下的卡片同色 —— 面直接消失。
        # 正确的对应是同方向的提亮：白色低透明度，原来越浓提得越多。
        return f"rgba(255, 255, 255, {_fmt(round(min(max(alpha * 0.16, 0.035), 0.09), 3))})"
    if max(rgb) <= 90 and _chroma(rgb) <= 30:
        return f"rgba(var(--ink-rgb), {_fmt(alpha)})"
    return None


def _tint(hue: float, alpha: float) -> str:
    """同色相的亮色按 alpha 水洗到面上：叠出来的底既有色相，又跟着皮肤走。"""
    rgb = _hex_rgb(_hsl_hex(hue, 0.60, 0.62))
    assert rgb is not None
    return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {alpha:g})"


def _fmt(alpha: float) -> str:
    return f"{alpha:g}"


def _hsl_hex(hue: float, sat: float, light: float) -> str:
    r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def covered(css: str) -> set[tuple[str, str]]:
    """暗色覆盖层里已经声明过的 (选择器, 属性)。"""
    done: set[tuple[str, str]] = set()
    for occ in parse(css):
        if not occ.selector.startswith((DARK_SCOPE, DARK_ROOT)):
            continue
        for sel in split_selectors(occ.selector):
            for prefix in (DARK_SCOPE, DARK_ROOT):
                if sel.startswith(prefix):
                    sel = normalize_selector(sel[len(prefix) :])
                    break
            done.add((sel, occ.prop))
    return done


def missing(css: str) -> list[Occurrence]:
    done = covered(css)
    out = []
    seen = set()
    for occ in parse(css):
        if not needs_dark_counterpart(occ):
            continue
        selectors = [s for s in split_selectors(occ.selector) if not is_dark_panel(s)]
        if all((sel, occ.prop) in done for sel in selectors):
            continue
        key = (occ.selector, occ.prop, occ.decl)
        if key in seen:
            continue
        seen.add(key)
        out.append(occ)
    return out


def emit(items: list[Occurrence]) -> str:
    blocks: dict[tuple[str, str], dict[str, str]] = {}
    for occ in items:
        decl = dark_value(occ).replace("( ", "(").replace(" )", ")")
        if decl == occ.decl.replace("( ", "(").replace(" )", ")"):
            continue
        sel = ",\n".join(
            f"{DARK_SCOPE} {s}"
            for s in split_selectors(occ.selector)
            if not is_dark_panel(s)
        )
        if not sel:
            continue
        blocks.setdefault((occ.at_rule, sel), {})[occ.prop] = decl + ";"
    lines: list[str] = []
    current_at = None
    for (at_rule, sel), decls in blocks.items():
        if at_rule != current_at:
            if current_at:
                lines.append("}")
            if at_rule:
                lines.append(f"{at_rule} {{")
            current_at = at_rule
        indent = "  " if at_rule else ""
        lines.append(f"{indent}{sel} {{")
        lines.extend(f"{indent}  {d}" for d in decls.values())
        lines.append(f"{indent}}}")
    if current_at:
        lines.append("}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit", action="store_true", help="打印缺失项的建议 CSS")
    args = parser.parse_args()

    if args.emit:
        # 只对 index.css 产出建议：页面样式表里的缺口人工搬到对应文件即可
        print(emit(missing(CSS.read_text(encoding="utf-8"))))
        return 0

    failures = 0
    for name, css in stylesheets():
        items = missing(css)
        if name != "index.css":
            # 只报这一页自己文件里的缺口，index.css 的部分由第一轮负责
            page_only = set(
                (o.line, o.selector, o.prop)
                for o in missing(CSS.read_text(encoding="utf-8"))
            )
            items = [o for o in items if (o.line, o.selector, o.prop) not in page_only]
        if not items:
            continue
        failures += len(items)
        print(f"dark-theme: {len(items)} 处亮色专属色值缺暗色覆盖（{name}）")
        for occ in items[:30]:
            print(f"  L{occ.line:<6} {occ.selector[:60]:60s} {occ.decl[:52]}")
        if len(items) > 30:
            print(f"  ... 另有 {len(items) - 30} 处")
    if failures:
        print("补覆盖：py scripts/check_dark_theme.py --emit")
        return 1
    print("dark-theme: 所有亮色专属色值都有暗色覆盖（index.css + 各页样式表）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
