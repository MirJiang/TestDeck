import pytest
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
