"""模块切分与画像的测试。

这些用例的价值全在**反例**上：monorepo 里最容易出的三类错
（把 vendor 当模块、按声明顺序截断、把库的导出入口当成产品入口），
都是在这个仓库上实测撞出来的。
"""

from __future__ import annotations

import json

import pytest

from agent_kit import modules


def _write(path, text: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _pkg(path, **fields):
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(json.dumps(fields), encoding="utf-8")
    return path


# ------------------------------------------------------------ 单包仓库


def test_single_package_repo_is_one_module(tmp_path):
    _write(tmp_path / "src" / "main.py", "print('hi')\n" * 30)
    _write(tmp_path / "README.md", "# 标题\n\n这是一个命令行工具。\n")
    scan = modules.scan_modules(tmp_path)
    assert len(scan.modules) == 1
    m = scan.modules[0]
    assert m.loc == 30
    assert m.has_readme
    assert "命令行工具" in m.readme_head


def test_plain_dir_without_markers(tmp_path):
    """没有任何标记文件的目录：不应该报 0 个模块。"""
    _write(tmp_path / "a.py", "x = 1\n" * 10)
    scan = modules.scan_modules(tmp_path)
    assert scan.strategy == "single"
    assert len(scan.modules) == 1


# ------------------------------------------------------------ workspace 声明


def test_pnpm_workspace_globs_are_expanded(tmp_path):
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    for name in ("cli", "web"):
        _pkg(tmp_path / "apps" / name, name=f"app-{name}")
        _write(tmp_path / "apps" / name / "index.js", "x\n" * 15)
    for name in ("core", "utils"):
        _pkg(tmp_path / "packages" / name, name=f"lib-{name}")
        _write(tmp_path / "packages" / name / "index.js", "x\n" * 20)

    scan = modules.scan_modules(tmp_path)
    assert scan.strategy == "workspace-declared"
    names = {m.name for m in scan.modules}
    assert names == {"app-cli", "app-web", "lib-core", "lib-utils"}


def test_package_json_workspaces_object_form(tmp_path):
    """package.json 的 workspaces 可以是数组，也可以是 {packages:[...]}。"""
    _pkg(tmp_path, name="root", workspaces={"packages": ["libs/*"]})
    _pkg(tmp_path / "libs" / "a", name="lib-a")
    _write(tmp_path / "libs" / "a" / "index.js", "x\n" * 10)
    scan = modules.scan_modules(tmp_path)
    assert {m.name for m in scan.modules} == {"lib-a"}


def test_vendor_is_never_counted_as_module(tmp_path):
    """vendor 里全是别人的代码。deepseek-harness 有 357 个 package.json，
    其中绝大多数在 vendor/**——扫进去等于把第三方代码当自己的模块。"""
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'vendor/*'\n")
    _pkg(tmp_path / "apps" / "cli", name="my-app")
    _write(tmp_path / "apps" / "cli" / "index.js", "x\n" * 30)
    _write(tmp_path / "vendor" / "thirdparty" / "package.json",
           json.dumps({"name": "not-ours"}))
    _write(tmp_path / "vendor" / "thirdparty" / "index.js", "x\n" * 100)

    scan = modules.scan_modules(tmp_path)
    assert [m.name for m in scan.modules] == ["my-app"]


def test_platform_artifact_packages_are_dropped(tmp_path):
    """native/system/packages/darwin-arm64 这类是预编译产物，没有源码。"""
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'native/*'\n")
    _pkg(tmp_path / "apps" / "cli", name="my-app")
    _write(tmp_path / "apps" / "cli" / "main.js", "x\n" * 20)
    _pkg(tmp_path / "native" / "darwin-arm64", name="prebuilt")
    _write(tmp_path / "native" / "darwin-arm64" / "binary.node", "\0")

    scan = modules.scan_modules(tmp_path)
    names = {m.name for m in scan.modules}
    assert "my-app" in names
    assert "prebuilt" not in names, "无源码的构建产物不该算模块"


# ------------------------------------------------------------ 入口识别


def test_library_export_is_not_runnable_entry(tmp_path):
    """`main: lib/index.js` 是库的导出，不是"能跑"的入口。

    早先把它算成 entry_points，结果 monorepo 里几乎每个包都"有入口"，
    这个信号直接失去区分度（实测 Top 14 全是 ★）。
    """
    _pkg(tmp_path / "lib", name="a-lib", main="lib/index.js",
         types="lib/index.d.ts")
    runnable, exports = modules._entry_points(tmp_path / "lib")
    assert runnable == ()
    assert exports and "lib/index.js" in exports[0]


def test_bin_field_counts_as_runnable(tmp_path):
    _pkg(tmp_path / "cli", name="a-cli", bin={"dsh": "lib/bin.js"})
    runnable, _ = modules._entry_points(tmp_path / "cli")
    assert "lib/bin.js" in runnable


def test_build_script_is_not_an_entry(tmp_path):
    """`scripts.build` 是构建脚本，不代表这个包能跑起来。"""
    _pkg(tmp_path / "site", name="website", scripts={"build": "vite build"})
    runnable, _ = modules._entry_points(tmp_path / "site")
    assert runnable == ()


def test_python_entrypoint_detected(tmp_path):
    _write(tmp_path / "main.py", "x\n" * 5)
    runnable, _ = modules._entry_points(tmp_path)
    assert "main.py" in runnable


# ------------------------------------------------------------ 评分


def test_importance_prefers_runnable_apps_over_widely_used_libs(tmp_path):
    """被几百个模块引用的基础库，不该把真正的应用挤出 Top N。"""
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    app = _pkg(tmp_path / "apps" / "desktop", name="desktop-app")
    _write(app / "main.ts", "x\n" * 3000)
    lib = _pkg(tmp_path / "packages" / "core", name="core-lib")
    _write(lib / "index.ts", "x\n" * 500)
    # 让 core 被大量模块依赖
    for i in range(30):
        dep = _pkg(tmp_path / "packages" / f"dep{i}", name=f"dep-{i}")
        _write(dep / "index.ts", "x\n" * 20)
        (dep / "package.json").write_text(
            json.dumps({"name": f"dep-{i}", "dependencies": {"core-lib": "*"}}), encoding="utf-8")

    scan = modules.scan_modules(tmp_path)
    names = [m.name for m in scan.top(5)]
    # 断言必须卡住"排第一"，不能只写 `in names`——
    # 后者太弱：线性权重下 core-lib 虽然排第一但同样在 top5 里，测试照样通过，
    # 等于这个反例根本没被验证到（故意破坏时才发现）。
    assert names[0] == "desktop-app", f"应用应排在被广泛引用的库之前，实际 top5={names}"


def test_cap_keeps_runnable_modules(tmp_path):
    """超出上限时，带入口的模块必须保留。"""
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    _pkg(tmp_path / "apps" / "cli", name="the-app")
    _write(tmp_path / "apps" / "cli" / "main.js", "x\n" * 4000)
    for i in range(30):
        _pkg(tmp_path / "packages" / f"p{i}", name=f"p-{i}")
        _write(tmp_path / "packages" / f"p{i}" / "index.js", "x\n" * (50 * (i + 1)))


    scan = modules.scan_modules(tmp_path, max_modules=5)
    assert len(scan.modules) <= 5
    assert any(m.name == "the-app" for m in scan.modules), "带入口的应用被截掉了"


# ------------------------------------------------------------ 依赖关系


def test_internal_dependency_links_both_ways(tmp_path):
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    core = _pkg(tmp_path / "packages" / "core", name="@x/core")
    _write(core / "index.js", "x\n" * 30)
    app = _pkg(tmp_path / "packages" / "app", name="@x/app",
               dependencies={"@x/core": "workspace:*"})
    _write(app / "index.js", "x\n" * 10)

    scan = modules.scan_modules(tmp_path)
    by_name = {m.name: m for m in scan.modules}
    assert "@x/core" in by_name["@x/app"].deps_internal
    assert "@x/app" in by_name["@x/core"].dependents


def test_nested_modules_are_not_double_counted(tmp_path):
    """monorepo 里子包套在父包目录下时，代码行不能算两遍。

    声明必须同时覆盖两层（apps/* 与 apps/*/*），否则子包根本不会被切成模块，
    这条断言就变成了「单模块统计」而非「嵌套去重」。
    """
    _write(tmp_path / "pnpm-workspace.yaml",
           "packages:\n  - 'apps/*'\n  - 'apps/*/*'\n")
    parent = _pkg(tmp_path / "apps" / "desktop", name="desktop")
    _write(parent / "main.ts", "x\n" * 100)
    # 注意：必须给到「目录路径」而不是父目录 —— _pkg 不会替你建子目录，
    # 传父目录会把父目录的 package.json 覆盖掉（这个坑踩过一次）
    child = _pkg(parent / "desktop-sub", name="desktop-sub")
    _write(child / "index.ts", "y\n" * 50)

    scan = modules.scan_modules(tmp_path)
    by_name = {m.name: m for m in scan.modules}
    assert set(by_name) == {"desktop", "desktop-sub"}, f"两个都该被切出：{list(by_name)}"
    assert by_name["desktop"].loc == 100, "父模块不应把子模块的代码算进来"
    assert by_name["desktop-sub"].loc == 50


# ------------------------------------------------------------ 序列化


def test_to_dict_is_json_serializable(tmp_path):
    _write(tmp_path / "a.py", "x = 1\n" * 5)
    _write(tmp_path / "README.md", "# t\n")
    data = modules.scan_modules(tmp_path).to_dict()
    json.dumps(data, ensure_ascii=False)   # 不抛异常即可
    assert data["module_count"] == 1
    assert set(data) >= {"strategy", "modules", "total_loc", "root_language"}


@pytest.mark.parametrize("empty", [pytest.param(True)])
def test_empty_repo_does_not_crash(tmp_path, empty):
    scan = modules.scan_modules(tmp_path)
    assert scan.modules  # 至少有一个（根）模块
    assert scan.total_loc == 0
