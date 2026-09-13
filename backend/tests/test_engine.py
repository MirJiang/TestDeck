from app.engine.runner import substitute, get_field, weak_eq, evaluate_check


def test_substitute():
    assert substitute("/api/${name}?p=1", {"name": "x"}) == "/api/x?p=1"
    assert substitute("/api/${unknown}", {}) == "/api/${unknown}"
    assert substitute("no vars", {"a": 1}) == "no vars"


def test_get_field():
    data = {"data": {"list": [{"id": 7}]}}
    assert get_field(data, "data.list.0.id") == 7
    assert get_field(data, "data.none") is None
    assert get_field(data, "") == data


def test_weak_eq():
    assert weak_eq("0", 0)
    assert not weak_eq("0", 1)


def test_check_status():
    ok = evaluate_check({"type": "status", "expect": "200"}, 200, "", None)
    bad = evaluate_check({"type": "status", "expect": "200"}, 500, "", None)
    assert ok["pass"] and not bad["pass"] and "500" in bad["reason"]


def test_check_contains():
    assert evaluate_check({"type": "contains", "expect": "登录成功"}, 200, "ok 登录成功", None)["pass"]


def test_check_field_eq():
    body = {"code": 0, "data": {"token": "t1"}}
    assert evaluate_check({"type": "field_eq", "field": "code", "expect": "0"}, 200, "", body)["pass"]
    r = evaluate_check({"type": "field_eq", "field": "data.token", "expect": "x"}, 200, "", body)
    assert not r["pass"] and "t1" in r["reason"]


def test_check_not_empty():
    assert evaluate_check({"type": "not_empty", "field": "data.list"}, 200, "", {"data": {"list": [1]}})["pass"]
    assert not evaluate_check({"type": "not_empty", "field": "data.list"}, 200, "", {"data": {"list": []}})["pass"]


def test_accelerate_video(tmp_path):
    """录像倍速压缩：时长按倍数缩短、体积明显下降，原文件路径不变。"""
    import re
    import subprocess
    from app.engine.ui_runner import _accelerate_video, _ffmpeg_exe
    exe = _ffmpeg_exe()
    if not exe:
        import pytest
        pytest.skip("无 ffmpeg，跳过倍速压缩测试")
    src = tmp_path / "v.webm"
    subprocess.run([exe, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=25",
                    "-c:v", "libvpx", "-b:v", "500k", str(src)], check=True)
    orig_size = src.stat().st_size

    def duration(p):
        r = subprocess.run([exe, "-i", str(p)], capture_output=True, text=True)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    assert 7 <= duration(src) <= 9
    _accelerate_video(src, speed=8)
    assert src.exists() and src.suffix == ".webm"        # 原路径替换，扩展名不变
    assert not list(tmp_path.glob("*.tmp.webm"))          # 临时文件已清理
    assert duration(src) < 2                              # 8 倍速后 8s → 1s 左右
    assert src.stat().st_size < orig_size                 # 体积同步下降
