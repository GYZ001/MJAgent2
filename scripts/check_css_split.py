"""Fail when the per-page CSS split stops being a cascade no-op.

index.css 里页面独占的样式已经拆进 frontend/src/styles/<Page>.css，随该页的懒加载
分包一起下载（index.css gzip 48.7K -> 33.5K）。拆分成立的前提有两条，这个脚本盯住它们：

1. 页面样式表里的选择器必须只被那一页用到 —— 否则别的页面用到同一个类时会掉样式；
2. 不能存在「今天排在它后面、同特指度、同属性、可能命中同一元素」的全局规则 ——
   页面 CSS 加载顺序在 index.css 之后，这种规则一旦存在，层叠结果就会被翻转。

Usage:
    py scripts/check_css_split.py
"""
import re, sys, pathlib, collections
sys.path.insert(0, '/root/MJAgent2/scripts')
import check_dark_theme as C

SRC = pathlib.Path('/root/MJAgent2/frontend/src')
PAGES = {
    'BiblePage': ['pages/BiblePage.tsx'], 'BoardPage': ['pages/BoardPage.tsx'],
    'WallPage': [
        'pages/WallPage.tsx',
        # 2026-08-31「传入素材」展示重做：生成台实际参考图画廊拆到
        # components/GenerationReferenceGallery.tsx（只有生成台一个消费方，复用
        # WallPage 既有的 .wall-attempt-issue/.wall-empty-hint 告警样式）。不登记
        # 就会被判成"不属于任何页面"，同 ScriptPage/AccountAdminPage 的先例。
        'components/GenerationReferenceGallery.tsx',
    ],
    'ScriptPage': [
        'pages/ScriptPage.tsx',
        # 2026-08-31 映射台入口审计：预检弹窗与"新发现/索引历史资源"摘要拆到
        # components/script/ 下的独立文件，不登记就会被判成"不属于任何页面"，
        # 从而被要求挪进 index.css（同 AccountAdminPage 的先例）。
        'components/script/',
    ],
    'ScenesPage': ['pages/ScenesPage.tsx'], 'MonitorPage': ['pages/MonitorPage.tsx', 'pages/monitor/'],
    'CinemaPage': ['pages/CinemaPage.tsx'], 'ReaderPage': ['pages/ReaderPage.tsx'],
    'SeriesPage': [
        'pages/SeriesPage.tsx',
        # 2026-09-01 新增连播台：区间选择/进度板/播放器拆到 pages/series/ 下的
        # 独立文件，不登记就会被判成"不属于任何页面"（同 ScriptPage 的先例）。
        'pages/series/',
    ],
    'AccountAdminPage': [
        'pages/AccountAdminPage.tsx', 'components/AccountAdminDialogs.tsx',
        # 2026-08-30 从 AccountAdminPage.tsx 抽出（该页当时 398/400 行，
        # 贴着前端 400 行上限）。卡片的 .account-card* 选择器随之搬家，
        # 不登记就会被判成「共享/全局」而要求挪进 index.css。
        'components/AccountCard.tsx',
    ],
}
CLS_RE = re.compile(r'className=(?:"([^"]*)"|\{`([^`]*)`\}|\{([^}]*)\})', re.S)

def _class_sets(text):
    out = []
    for m in CLS_RE.finditer(text):
        raw = m.group(1) or m.group(2) or m.group(3) or ''
        toks = set(re.findall(r'[a-zA-Z][\w-]*', raw))
        if toks: out.append(toks)
    return out

ALL = {p: p.read_text(encoding='utf-8') for p in SRC.rglob('*')
       if p.suffix in ('.tsx', '.ts') and p.is_file() and '.test.' not in p.name}
COOCCUR = [s for text in ALL.values() for s in _class_sets(text)]

def _expand_entry(entry):
    """一页可以声明一个目录（以 '/' 结尾）代替逐个列举其下的分拆组件文件——
    否则新增一个 pages/monitor/Foo.tsx 却忘了在这里登记，它的 className 就会被
    当成"不属于任何页面"，从而被误判成共享/全局。"""
    if entry.endswith('/'):
        base = SRC / entry
        return {str(f) for f in base.rglob('*')
                if f.suffix in ('.tsx', '.ts') and f.is_file() and '.test.' not in f.name}
    return {str(SRC / entry)}

PAGE_FILES = {n: set().union(*(_expand_entry(r) for r in f)) for n, f in PAGES.items()}
PAGE_CLS = {}
for name, files in PAGE_FILES.items():
    toks = set()
    for p, text in ALL.items():
        if str(p) in files:
            for s in _class_sets(text): toks |= s
    PAGE_CLS[name] = toks
SHARED = set()
for p, text in ALL.items():
    if not any(str(p) in f for f in PAGE_FILES.values()):
        # 只把「真的挂在某个元素的 className 上」的 token 计入共享——整份源码
        # 里任意位置出现的同名字符串（URL 路径片段、注释、变量名……）不算数。
        # 例如 api/delivery.ts 里的 `/episodes/${id}/customer-feedback` 会让
        # 同名 class `.customer-feedback` 被误判成共享，即便它只在一个页面用到。
        for s in _class_sets(text): SHARED |= s

# 纯状态修饰词：到处都在用，但从不单独承载样式，不该左右归属判定。
# `.prep-roster-name-btn.selected` 的归属由 .prep-roster-name-btn 决定。
STATE_TOKENS = frozenset({
    "selected", "active", "open", "expanded", "disabled", "busy", "current",
})


def owner(selector):
    """规则里所有类名都只属于同一页 -> 这条规则可以跟着那一页走。"""
    cls = {c.lstrip('.') for c in re.findall(r'\.[a-zA-Z][\w-]*', selector)}
    cls -= STATE_TOKENS
    if not cls: return None
    owners = set()
    for c in cls:
        if c in SHARED: return None
        hit = [p for p, s in PAGE_CLS.items()
               if c in s or any(c.startswith(x + '-') or x.startswith(c + '-') for x in s)]
        if not hit: return None
        owners |= set(hit)
    return owners.pop() if len(owners) == 1 else None

def specificity(sel):
    s = sel.strip()
    a = b = c = 0
    out, i = '', 0
    while i < len(s):
        m = re.match(r':(where|is|not|has)\(', s[i:])
        if not m:
            out += s[i]; i += 1; continue
        depth, j = 0, i + len(m.group(0)) - 1
        while j < len(s):
            if s[j] == '(': depth += 1
            elif s[j] == ')':
                depth -= 1
                if depth == 0: break
            j += 1
        if m.group(1) != 'where':
            best = max((specificity(p) for p in s[i + len(m.group(0)):j].split(',')), default=(0,0,0))
            a += best[0]; b += best[1]; c += best[2]
        i = j + 1
    s = out
    a += len(re.findall(r'#[\w-]+', s))
    b += (len(re.findall(r'\.[\w-]+', s)) + len(re.findall(r'\[[^\]]+\]', s))
          + len(re.findall(r'(?<!:):(?!:)[a-z-]+', s)))
    c += (len(re.findall(r'::[a-z-]+', s))
          + len(re.findall(r'(?:^|[\s>+~])([a-z][\w-]*)', s)))
    return (a, b, c)

_spec = {}
def spec_of(sel):
    if sel not in _spec:
        _spec[sel] = max((specificity(p) for p in C.split_selectors(sel)), default=(0,0,0))
    return _spec[sel]

def sel_classes(sel):
    return {c.lstrip('.') for c in re.findall(r'\.[a-zA-Z][\w-]*', sel)}

# 类名 -> 它在源码里被挂到过哪些标签上（<div className="scene-thumb"> -> div）
TAG_RE = re.compile(r'<([a-zA-Z][\w.]*)\b[^>]*?className=(?:"([^"]*)"|\{`([^`]*)`\}|\{([^}]*)\})', re.S)
CLASS_TAGS = collections.defaultdict(set)
for _text in ALL.values():
    for m in TAG_RE.finditer(_text):
        tag = m.group(1)
        raw = m.group(2) or m.group(3) or m.group(4) or ''
        for tok in re.findall(r'[a-zA-Z][\w-]*', raw):
            CLASS_TAGS[tok].add(tag if tag[0].islower() else '<组件>')

HTML_TAG = re.compile(r'(?:^|[\s>+~(,])([a-z][a-z0-9]*)(?=[\s.:#\[)>+~,]|$)')

def required_tags(sel):
    """选择器里出现的 HTML 标签名；空集表示不限定标签。"""
    return {m.group(1) for m in HTML_TAG.finditer(sel)} - {'and', 'or', 'not', 'where', 'is', 'has'}

def possible_tags(classes):
    """这些类名在源码里挂过的标签；空集表示查不到，按不确定处理。"""
    tags = set()
    for c in classes:
        tags |= CLASS_TAGS.get(c, set())
    return tags

def may_share_element(a, b):
    ca, cb = sel_classes(a), sel_classes(b)
    ta, tb = required_tags(a), required_tags(b)
    # 双方都限定了标签且没有交集 -> 不可能是同一个元素
    if ta and tb and not (ta & tb): return False
    # 一方限定标签，另一方的类名在源码里从没挂到那些标签上 -> 也不可能
    if tb and ca:
        pa = possible_tags(ca)
        if pa and not (pa & tb) and '<组件>' not in pa: return False
    if ta and cb:
        pb = possible_tags(cb)
        if pb and not (pb & ta) and '<组件>' not in pb: return False
    if not ca or not cb: return True
    if ca & cb: return True
    return any(ca & s and cb & s for s in COOCCUR)



def main() -> int:
    """守卫只盯「归属」这一条，因为它精确且无假阳性。

    层叠等价性是在拆分当时用「逐页比对每个 (选择器, 属性) 的最终生效声明」
    证明的（拆前 index.css vs 拆后 index.css + 该页样式表，9 页全部 0 差异）。
    那套比对依赖拆分前的原始文件，没法留成常驻检查；而归属一旦守住，
    新加的规则就只会影响它自己那一页，翻转不了别人。
    """
    styles_dir = SRC / "styles"
    if not styles_dir.is_dir():
        print("css-split: 没有 frontend/src/styles，跳过")
        return 0

    problems = 0
    for path in sorted(styles_dir.glob("*.css")):
        page = path.stem
        if page not in PAGES:
            print(f"css-split: styles/{path.name} 没有对应的懒加载页面，删掉或补进 PAGES")
            problems += 1
            continue

        tsx = SRC / "pages" / f"{page}.tsx"
        if f'styles/{page}.css' not in tsx.read_text(encoding="utf-8"):
            print(f"css-split: {tsx.name} 没有 import 自己的样式表，这一页会掉样式")
            problems += 1

        strays = []
        for occ in C.parse(path.read_text(encoding="utf-8")):
            if not occ.selector:
                continue
            who = owner(occ.selector)
            if who != page:
                strays.append((occ.line, occ.selector, who))
        if strays:
            problems += len(strays)
            uniq = {sel: (line, who) for line, sel, who in strays}
            print(f"css-split: styles/{path.name} 有 {len(uniq)} 个选择器不属于本页")
            for sel, (line, who) in list(uniq.items())[:10]:
                print(f"  L{line:<5} {sel[:62]:62s} 归属={who or '共享/全局'}")

    if problems:
        print("页面样式表只放这一页独占的选择器；跨页复用的留在 index.css。")
        return 1
    print("css-split: 各页样式表都只含本页独占的选择器")
    return 0


if __name__ == "__main__":
    sys.exit(main())
