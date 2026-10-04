"""模块摘要的测试。

重点是**长度必须可控**——摘要要进 prompt，超长会把代码挤出上下文。
其余用例盯住"抽取规则对不对"，因为抽错了模型就会说错模块职责。
"""

from __future__ import annotations

import json

from agent_kit import modules
from agent_kit.module_digest import build_module_digest


def _w(path, text: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ts_module(root, name="my-app", with_readme=True):
    mod = root / "apps" / name
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "package.json").write_text(
        json.dumps({"name": name, "bin": {name: "src/main.ts"}}), encoding="utf-8")
    _w(mod / "src" / "main.ts",
       "import {a} from './a';\nexport class App {}\nexport function run() {}\n"
       "export interface Cfg {}\nexport const V = '1';\nexport function helper() {}\n")
    _w(mod / "src" / "util.ts", "export const t = 1;\n" * 200)
    if with_readme:
        _w(mod / "README.md", "# 标题\n\n这个模块负责命令行启动与参数解析。\n")
    return mod


# ------------------------------------------------------------ 长度控制


def test_digest_respects_max_chars(tmp_path, monkeypatch):
    """整体上限必须生效，且截断时要说明省略了多少。

    注意：正常情况下**不会**触发截断——各段自己就有硬上限
    （40 个符号、600 字自述、60 条目录项）。所以这条测的是**兜底**：
    将来放宽某段上限时，整体上限不能跟着失效。
    """
    import agent_kit.module_digest as md

    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    mod = _ts_module(tmp_path)
    symbols = "\n".join(f"export function fn{i}() {{}}" for i in range(300))
    _w(mod / "src" / "many.ts", symbols)
    _w(mod / "README.md", "# 标题\n\n" + ("这是一段很长的自述内容。" * 300))

    # 放宽段上限，只留整体上限在守
    monkeypatch.setattr(md, "_MAX_SYMBOLS", 2000)
    scan = modules.scan_modules(tmp_path)
    d = build_module_digest(tmp_path, scan.modules[0], max_chars=1200)
    assert len(d) <= 1200 + 80, f"摘要超长：{len(d)}"
    # 被截断时必须说明省略了多少，否则模型会以为看到的是全部
    assert "省略" in d


def test_digest_has_all_sections(tmp_path):
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    _ts_module(tmp_path)
    scan = modules.scan_modules(tmp_path)
    d = build_module_digest(tmp_path, scan.modules[0])
    for section in ("【画像】", "【结构】", "【对外提供】", "【自述】"):
        assert section in d, f"缺 {section}"


def test_digest_omits_truncation_note_when_short(tmp_path):
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    _ts_module(tmp_path)
    scan = modules.scan_modules(tmp_path)
    assert "省略" not in build_module_digest(tmp_path, scan.modules[0])


# ------------------------------------------------------------ 符号抽取


def test_exports_extracted_not_privates(tmp_path):
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    _ts_module(tmp_path)
    scan = modules.scan_modules(tmp_path)
    d = build_module_digest(tmp_path, scan.modules[0])
    assert "App" in d and "run" in d
    assert "a" not in d.split("【对外提供】")[1].split("\n")[0].replace("App", "")


def test_python_symbols_extracted(tmp_path):
    _w(tmp_path / "app.py", "def main():\n    pass\n\nclass Engine:\n    pass\n\ndef _priv():\n    pass\n")
    scan = modules.scan_modules(tmp_path)
    d = build_module_digest(tmp_path, scan.modules[0])
    line = d.split("【对外提供】")[1].split("\n")[0]
    assert "main" in line and "Engine" in line
    assert "_priv" not in line, "私有名词不该出现在对外提供里"


def test_go_exported_symbols_only(tmp_path):
    _w(tmp_path / "go.mod", "module x\n")
    _w(tmp_path / "main.go", "package main\nfunc Exported() {}\nfunc unexported() {}\n")
    scan = modules.scan_modules(tmp_path)
    d = build_module_digest(tmp_path, scan.modules[0])
    line = d.split("【对外提供】")[1].split("\n")[0]
    assert "Exported" in line
    assert "unexported" not in line, "Go 里小写开头是私有的，不该算对外提供"


# ------------------------------------------------------------ 测试目录折叠


def test_test_directories_are_collapsed(tmp_path):
    """测试文件名单对判断职责没帮助，展开会挤掉 src/。"""
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    mod = _ts_module(tmp_path)
    for i in range(25):
        _w(mod / "tests" / f"spec{i}.e2e.ts", "it('x', () => {})\n")
    scan = modules.scan_modules(tmp_path)
    d = build_module_digest(tmp_path, scan.modules[0], max_chars=2600)
    tree = d.split("【结构】")[1].split("【")[0]
    assert "spec0.e2e.ts" not in tree, "测试文件名不该逐个列出"
    assert "tests/" in tree and "测试/夹具" in tree
    assert "main.ts" in tree, "src 下的文件必须保留"


def test_node_modules_never_listed(tmp_path):
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    mod = _ts_module(tmp_path)
    _w(mod / "node_modules" / "left-pad" / "index.js", "module.exports=1\n")
    scan = modules.scan_modules(tmp_path)
    d = build_module_digest(tmp_path, scan.modules[0])
    assert "left-pad" not in d


# ------------------------------------------------------------ 边界


def test_missing_module_dir_does_not_crash(tmp_path):
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    _ts_module(tmp_path)
    scan = modules.scan_modules(tmp_path)
    prof = scan.modules[0]
    prof.path = "apps/does-not-exist"
    d = build_module_digest(tmp_path, prof)
    assert "不存在" in d


def test_empty_module_gives_digest(tmp_path):
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    mod = tmp_path / "apps" / "empty"
    mod.mkdir(parents=True)
    _w(mod / "package.json", json.dumps({"name": "empty"}))
    _w(mod / "index.ts", "export const a = 1;\n")
    scan = modules.scan_modules(tmp_path)
    d = build_module_digest(tmp_path, scan.modules[0])
    assert "empty" in d


def test_build_digests_survives_one_failure(tmp_path, monkeypatch):
    """一个模块摘要失败不该拖垮整轮——真实场景是某个目录权限异常。"""
    _w(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'libs/*'\n")
    _ts_module(tmp_path, "a")
    (tmp_path / "libs" / "b").mkdir(parents=True)
    _w(tmp_path / "libs" / "b" / "package.json", json.dumps({"name": "b"}))
    _w(tmp_path / "libs" / "b" / "index.ts", "export const b = 1;\n")
    scan = modules.scan_modules(tmp_path)
    assert len(scan.modules) == 2

    import agent_kit.module_digest as md

    real = md.build_module_digest
    calls = {"n": 0}

    def flaky(root, profile, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("模拟读取失败")
        return real(root, profile, **kw)

    monkeypatch.setattr(md, "build_module_digest", flaky)
    out = md.build_digests(tmp_path, scan.modules)
    assert len(out) == 2, "失败也要占位，不能少一个模块"
    assert any("失败" in v for v in out.values())
    assert any("画像" in v for v in out.values()), "其余模块应正常产出"
