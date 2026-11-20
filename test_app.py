# -*- coding: utf-8 -*-
"""标记重捕估算系统的端到端测试（Flask test client + 临时 SQLite）。"""

import importlib
import io
import json
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c, app_mod


def csv_bytes(text):
    return io.BytesIO(text.encode("utf-8"))


def upload(client, r1, r2):
    return client.post(
        "/upload",
        data={
            "round1_file": (csv_bytes(r1), "round1.csv"),
            "round2_file": (csv_bytes(r2), "round2.csv"),
        },
        content_type="multipart/form-data",
    )


R1 = "tag\nA\nB\nC\nD\n"
R2 = (
    "sample_no,status,candidates\n"
    "s1,有标,A\n"
    "s2,有标,A;B\n"
    "s3,看不清,B;C\n"
    "s4,无标,\n"
    "s5,看不清,\n"
)


def get_est(app_mod):
    conn = app_mod.get_db()
    row = conn.execute("SELECT * FROM estimate WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def test_upload_feasible_and_estimate(client):
    c, app_mod = client
    resp = upload(c, R1, R2)
    assert resp.status_code == 302  # 成功 → 回首页
    est = get_est(app_mod)
    assert est["n1"] == 4 and est["n2"] == 5
    # 有标 2 条必配 → 最少重捕 2；s3 可再配 → 最多重捕 3
    assert est["min_recap"] == 2
    assert est["max_recap"] == 3
    # N = 4*5/m：上限 10.0（m=2），下限 20/3（m=3）
    assert est["est_high"] == pytest.approx(10.0)
    assert est["est_low"] == pytest.approx(20 / 3)
    min_pairs = {p["sample_no"]: p["tag"] for p in json.loads(est["min_matching"])}
    assert min_pairs == {"s1": "A", "s2": "B"}
    max_pairs = {p["sample_no"]: p["tag"] for p in json.loads(est["max_matching"])}
    assert len(max_pairs) == 3 and max_pairs["s1"] == "A" and max_pairs["s2"] == "B"


def test_infeasible_upload_keeps_old_state(client):
    c, app_mod = client
    assert upload(c, R1, R2).status_code == 302
    # 两个有标样本抢同一个候选号 → 霍尔证书：2 个样本、1 个候选号
    bad_r2 = (
        "sample_no,status,candidates\n"
        "x1,有标,A\n"
        "x2,有标,A\n"
        "x3,无标,\n"
    )
    resp = upload(c, R1, bad_r2)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "无可行配对" in body and "x1" in body and "x2" in body
    # 旧记录与估算原样保留
    est = get_est(app_mod)
    assert est["n2"] == 5 and est["min_recap"] == 2
    conn = app_mod.get_db()
    names = [r[0] for r in conn.execute("SELECT sample_no FROM round2")]
    conn.close()
    assert names == ["s1", "s2", "s3", "s4", "s5"]


def test_correct_success_bumps_version_and_saves(client):
    c, app_mod = client
    upload(c, R1, R2)
    # s5 由无候选的"看不清"改为候选 D → 最多重捕变 4，下限变 5.0
    resp = c.post(
        "/correct",
        data={"sample_no": "s5", "version": "1",
              "status": "看不清", "candidates": "D"},
    )
    assert resp.status_code == 302
    est = get_est(app_mod)
    assert est["max_recap"] == 4
    assert est["est_low"] == pytest.approx(5.0)
    conn = app_mod.get_db()
    row = conn.execute(
        "SELECT version, candidates FROM round2 WHERE sample_no='s5'"
    ).fetchone()
    conn.close()
    assert row["version"] == 2
    assert json.loads(row["candidates"]) == ["D"]


def test_correct_stale_version_rejected(client):
    c, app_mod = client
    upload(c, R1, R2)
    c.post("/correct", data={"sample_no": "s5", "version": "1",
                             "status": "看不清", "candidates": "D"})
    # 用旧版号再次提交 → 过期，估算不变
    resp = c.post(
        "/correct",
        data={"sample_no": "s5", "version": "1",
              "status": "无标", "candidates": ""},
    )
    assert resp.status_code == 409
    assert "已过期" in resp.get_data(as_text=True)
    est = get_est(app_mod)
    assert est["max_recap"] == 4  # 仍是第一次更正后的结果


def test_correct_to_infeasible_rejected(client):
    c, app_mod = client
    upload(c, R1, R2)
    # 把 s2 的候选改成只有 A（与 s1 冲突）→ 无解，保留旧估算
    resp = c.post(
        "/correct",
        data={"sample_no": "s2", "version": "1",
              "status": "有标", "candidates": "A"},
    )
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "无可行配对" in body and "s1" in body and "s2" in body
    est = get_est(app_mod)
    assert est["min_recap"] == 2 and est["max_recap"] == 3


def test_zero_recapture_marked_unestimable(client):
    c, app_mod = client
    r2 = "sample_no,status,candidates\ns1,无标,\ns2,看不清,\n"
    assert upload(c, R1, r2).status_code == 302
    est = get_est(app_mod)
    assert est["min_recap"] == 0 and est["max_recap"] == 0
    assert est["est_high"] is None and est["est_low"] is None
    resp = c.get("/")
    assert "不可估" in resp.get_data(as_text=True)


def test_persistence_across_restart(client, tmp_path, monkeypatch):
    c, app_mod = client
    upload(c, R1, R2)
    c.post("/correct", data={"sample_no": "s5", "version": "1",
                             "status": "看不清", "candidates": "D"})
    # 模拟重启：同一 DB 路径重新导入应用
    sys.modules.pop("app", None)
    app_mod2 = importlib.import_module("app")
    app_mod2.app.config["TESTING"] = True
    with app_mod2.app.test_client() as c2:
        body = c2.get("/").get_data(as_text=True)
        assert "s5" in body and "当前估算" in body
    est = get_est(app_mod2)
    assert est["max_recap"] == 4 and est["est_low"] == pytest.approx(5.0)


def test_validation_errors(client):
    c, app_mod = client
    # 无标却带候选号
    resp = upload(c, R1, "sample_no,status,candidates\ns1,无标,A\n")
    assert resp.status_code == 400
    assert "无标" in resp.get_data(as_text=True)
    # 候选号不在首轮
    resp = upload(c, R1, "sample_no,status,candidates\ns1,有标,Z\n")
    assert resp.status_code == 400
    assert "不在首轮" in resp.get_data(as_text=True)
    # 超过 300 条
    big = "tag\n" + "".join(f"T{i}\n" for i in range(301))
    resp = upload(c, big, "sample_no,status,candidates\ns1,无标,\n")
    assert resp.status_code == 400
    assert "1~300" in resp.get_data(as_text=True)
    # 首轮标记号重复
    resp = upload(c, "tag\nA\nA\n", "sample_no,status,candidates\ns1,无标,\n")
    assert resp.status_code == 400
    assert "不唯一" in resp.get_data(as_text=True)
    # 全部失败 → 数据库仍为空
    assert get_est(app_mod) is None


def test_max_rows_boundary(client):
    c, app_mod = client
    r1 = "tag\n" + "".join(f"T{i}\n" for i in range(300))
    r2 = "sample_no,status,candidates\n" + "".join(
        f"s{i},有标,T{i}\n" for i in range(300)
    )
    assert upload(c, r1, r2).status_code == 302
    est = get_est(app_mod)
    assert est["min_recap"] == 300 and est["max_recap"] == 300
    assert est["est_high"] == pytest.approx(300.0)


# ------------------------------------------------------------ 外部复算包

def create_package(client):
    resp = client.post("/recap/package")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    cd = resp.headers["Content-Disposition"]
    assert "attachment" in cd and "recap_package_" in cd
    return json.loads(resp.get_data(as_text=True))


def verify_upload(client, doc):
    return client.post(
        "/recap/verify",
        data={"recap_file": (io.BytesIO(
            json.dumps(doc, ensure_ascii=False).encode("utf-8")), "result.json")},
        content_type="multipart/form-data",
    )


def test_package_download_renumbers_and_keeps_originals_local(client):
    c, app_mod = client
    upload(c, R1, R2)
    payload = create_package(c)
    # 包里只有包号和换过代号的两轮记录
    assert set(payload.keys()) == {"package_no", "round1", "round2"}
    assert payload["package_no"].startswith("RP-")
    assert len(payload["package_no"]) == 3 + 8
    # 现场原号绝不进包：逐一核对每个代号都不是原号，原始标记/样本集合无泄漏
    snap0 = _snapshot(app_mod, payload["package_no"])
    sent_tags = {item["tag"] for item in payload["round1"]}
    sent_samples = {r["sample_no"] for r in payload["round2"]}
    assert sent_tags.isdisjoint(set(snap0["round1"]))
    assert sent_samples.isdisjoint({r["sample_no"] for r in snap0["round2"]})
    sent_codes = sent_tags | sent_samples | {
        c for r in payload["round2"] for c in r["candidates"]
    }
    assert all(code.startswith(("TAG-", "SMP-")) for code in sent_codes)
    # 首轮只剩代号
    assert all(set(item.keys()) == {"tag"} for item in payload["round1"])
    tag_codes = [item["tag"] for item in payload["round1"]]
    assert all(t.startswith("TAG-") for t in tag_codes) and len(set(tag_codes)) == 4
    # 次轮状态与候选关系照录（条数、状态分布、候选条数一致；次序已随机打乱）
    assert len(payload["round2"]) == 5
    assert Counter(r["status"] for r in payload["round2"]) == Counter(
        {"有标": 2, "无标": 1, "看不清": 2}
    )
    total_cands = sum(len(r["candidates"]) for r in payload["round2"])
    assert total_cands == 1 + 2 + 2  # s1=1, s2=2, s3=2
    assert all(r["sample_no"].startswith("SMP-") for r in payload["round2"])
    # 留存表含对应表与核验依据
    conn = app_mod.get_db()
    row = conn.execute(
        "SELECT snapshot, file_json, max_recap FROM recap_package"
        " WHERE package_no = ?",
        (payload["package_no"],),
    ).fetchone()
    conn.close()
    assert row is not None and row["max_recap"] == 3
    snap = json.loads(row["snapshot"])
    assert set(snap.keys()) == {
        "package_no", "max_recap", "tag_map", "sample_map", "round1", "round2"
    }
    # 留存文件与下载文件逐字节一致（同一已提交状态）
    assert row["file_json"] == json.dumps(payload, ensure_ascii=False, indent=2)


def test_each_package_gets_fresh_code_and_mapping(client):
    c, app_mod = client
    upload(c, R1, R2)
    p1 = create_package(c)
    p2 = create_package(c)
    assert p1["package_no"] != p2["package_no"]
    # 代号带包号派生的前缀，两个包的代号互不相同
    s1 = _snapshot(app_mod, p1["package_no"])
    s2 = _snapshot(app_mod, p2["package_no"])
    codes1 = set(s1["tag_map"].values()) | set(s1["sample_map"].values())
    codes2 = set(s2["tag_map"].values()) | set(s2["sample_map"].values())
    assert codes1.isdisjoint(codes2)
    # 代号是原号的一一换号（双向唯一），且都不与原号重名
    assert len(s1["tag_map"]) == len(set(s1["tag_map"].values())) == 4
    assert len(s1["sample_map"]) == len(set(s1["sample_map"].values())) == 5
    conn = app_mod.get_db()
    count = conn.execute("SELECT COUNT(*) AS k FROM recap_package").fetchone()["k"]
    conn.close()
    assert count == 2


def test_package_requires_committed_survey(client):
    c, app_mod = client
    resp = c.post("/recap/package")
    assert resp.status_code == 400
    assert "无法生成复算包" in resp.get_data(as_text=True)
    conn = app_mod.get_db()
    assert conn.execute("SELECT COUNT(*) AS k FROM recap_package").fetchone()["k"] == 0
    conn.close()


def test_retention_failure_blocks_download(client, monkeypatch):
    c, app_mod = client
    upload(c, R1, R2)
    real_get_db = app_mod.get_db

    class FailingConn:
        """留存写入（INSERT recap_package）抛错，其余调用透传真连接。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT INTO RECAP_PACKAGE"):
                raise app_mod.sqlite3.Error("disk full simulation")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def flaky_get_db():
        return FailingConn(real_get_db())

    monkeypatch.setattr(app_mod, "get_db", flaky_get_db)
    resp = c.post("/recap/package")
    assert resp.status_code == 500
    assert "留存失败" in resp.get_data(as_text=True)
    monkeypatch.undo()
    conn = app_mod.get_db()
    assert conn.execute("SELECT COUNT(*) AS k FROM recap_package").fetchone()["k"] == 0
    # 调查记录与估算未受影响
    assert get_est(app_mod)["max_recap"] == 3
    conn.close()


def _snapshot(app_mod, package_no):
    conn = app_mod.get_db()
    row = conn.execute(
        "SELECT snapshot FROM recap_package WHERE package_no = ?", (package_no,)
    ).fetchone()
    conn.close()
    return json.loads(row["snapshot"])


def _orig_to_code(snapshot):
    return snapshot["tag_map"], snapshot["sample_map"]


def test_verify_accepts_any_legal_maximum_matching(client):
    c, app_mod = client
    upload(c, R1, R2)
    payload = create_package(c)
    snap = _snapshot(app_mod, payload["package_no"])
    tag_code, sample_code = _orig_to_code(snap)
    # 一套与本站不同但同样合法的最大配对（3 对）：s1-A, s2-A 冲突，故取 s1-A,s2-B,s3-C
    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
            {"sample_no": sample_code["s2"], "tag": tag_code["B"]},
            {"sample_no": sample_code["s3"], "tag": tag_code["C"]},
        ],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "核验通过" in body


def test_verify_legal_but_extendable_gives_simple_add_step(client):
    c, app_mod = client
    upload(c, R1, R2)
    payload = create_package(c)
    snap = _snapshot(app_mod, payload["package_no"])
    tag_code, sample_code = _orig_to_code(snap)
    # 只交最少重捕的 2 对（合法但还能多配）；s3 可直接补 C，无需撤配对
    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
            {"sample_no": sample_code["s2"], "tag": tag_code["B"]},
        ],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "还能多配" in body and "补上配对" in body
    assert "样本 s3" in body and "标记 C" in body
    assert "撤掉配对" not in body  # 该增广路只需补、不需撤


def test_verify_extendable_step_with_remove_and_add_uses_original_numbers(client):
    c, app_mod = client
    r1 = "tag\nA\nB\nC\n"
    r2 = (
        "sample_no,status,candidates\n"
        "s1,有标,A\n"
        "s2,有标,B;C\n"
        "s3,看不清,B\n"
        "s4,无标,\n"
    )
    upload(c, r1, r2)
    payload = create_package(c)
    snap = _snapshot(app_mod, payload["package_no"])
    assert snap["max_recap"] == 3
    tag_code, sample_code = _orig_to_code(snap)
    # 交 2 对：s1-A, s2-B。s3 只能配 B，须把 s2 换到 C：
    # 撤 s2-B，补 s3-B 与 s2-C
    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
            {"sample_no": sample_code["s2"], "tag": tag_code["B"]},
        ],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "还能多配" in body
    assert "撤掉配对：样本 s2 — 标记 B" in body
    assert "补上配对：样本 s3 — 标记 B" in body
    assert "补上配对：样本 s2 — 标记 C" in body
    # 页面上出现的全是原号，不出现包内代号
    assert "TAG-" not in body and "SMP-" not in body


def test_verify_rejects_untagged_pair_candidate_violation_and_missing_tagged(client):
    c, app_mod = client
    upload(c, R1, R2)
    payload = create_package(c)
    snap = _snapshot(app_mod, payload["package_no"])
    tag_code, sample_code = _orig_to_code(snap)

    # 无标不配：给无标样本 s4 配对
    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s4"], "tag": tag_code["D"]},
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
            {"sample_no": sample_code["s2"], "tag": tag_code["B"]},
            {"sample_no": sample_code["s3"], "tag": tag_code["C"]},
        ],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "核验不通过" in body and "无标样本 s4" in body and "无标不配" in body

    # 候选关系：s3 只可能配 B/C，配 D 即越界（D 未被占用，避免与“标记重复”规则混淆）
    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
            {"sample_no": sample_code["s2"], "tag": tag_code["B"]},
            {"sample_no": sample_code["s3"], "tag": tag_code["D"]},
        ],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "核验不通过" in body
    assert "不在候选关系内" in body and "样本 s3" in body and "标记 D" in body

    # 有标必配：漏掉有标样本 s2 —— 配对本身无冲突，归入“还能多配”，
    # 增配步骤以原号指出如何把 s2 补进来
    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
        ],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "还能多配" in body and "样本 s2" in body


def test_verify_rejects_duplicate_sample_and_tag(client):
    c, app_mod = client
    upload(c, R1, R2)
    payload = create_package(c)
    snap = _snapshot(app_mod, payload["package_no"])
    tag_code, sample_code = _orig_to_code(snap)
    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
            {"sample_no": sample_code["s1"], "tag": tag_code["B"]},
        ],
    }
    body = verify_upload(c, doc).get_data(as_text=True)
    assert "同一样本被使用多次" in body and "s1" in body

    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
            {"sample_no": sample_code["s2"], "tag": tag_code["A"]},
        ],
    }
    body = verify_upload(c, doc).get_data(as_text=True)
    assert "同一标记被使用多次" in body and "标记" in body and "A" in body


def test_verify_rejects_foreign_codes_and_unknown_package(client):
    c, app_mod = client
    upload(c, R1, R2)
    payload = create_package(c)
    snap = _snapshot(app_mod, payload["package_no"])
    tag_code, sample_code = _orig_to_code(snap)

    # 不属于本包的代号
    doc = {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": "TAG-999"},
        ],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 422
    assert "不属于本包" in resp.get_data(as_text=True)

    # 未留存的包号
    doc = {"package_no": "RP-00000000", "pairs": []}
    resp = verify_upload(c, doc)
    assert resp.status_code == 404
    assert "未在本站留存" in resp.get_data(as_text=True)

    # 第二个包的代号混进第一个包 → 只收本包代号
    other = create_package(c)
    other_snap = _snapshot(app_mod, other["package_no"])
    foreign_sample = other_snap["sample_map"]["s1"]
    doc = {
        "package_no": payload["package_no"],
        "pairs": [{"sample_no": foreign_sample, "tag": tag_code["A"]}],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 422
    assert foreign_sample in resp.get_data(as_text=True)


def test_verify_malformed_json_and_missing_fields(client):
    c, app_mod = client
    upload(c, R1, R2)
    create_package(c)
    # 非法 JSON
    resp = c.post(
        "/recap/verify",
        data={"recap_file": (io.BytesIO(b"{not json"), "x.json")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400 and "合法的 UTF-8 JSON" in resp.get_data(as_text=True)
    # 缺 package_no
    resp = verify_upload(c, {"pairs": []})
    assert resp.status_code == 400 and "package_no" in resp.get_data(as_text=True)
    # pairs 不是数组
    resp = verify_upload(c, {"package_no": "RP-X", "pairs": {}})
    assert resp.status_code == 400 and "pairs 须为数组" in resp.get_data(as_text=True)
    # 未选文件
    resp = c.post("/recap/verify")
    assert resp.status_code == 400 and "请选择" in resp.get_data(as_text=True)


def test_verify_does_not_change_survey_or_estimate(client):
    c, app_mod = client
    upload(c, R1, R2)
    before = get_est(app_mod)
    payload = create_package(c)
    snap = _snapshot(app_mod, payload["package_no"])
    tag_code, sample_code = _orig_to_code(snap)
    verify_upload(c, {
        "package_no": payload["package_no"],
        "pairs": [
            {"sample_no": sample_code["s4"], "tag": tag_code["D"]},
        ],
    })
    after = get_est(app_mod)
    assert dict(before) == dict(after)
    conn = app_mod.get_db()
    names = [r[0] for r in conn.execute("SELECT sample_no FROM round2")]
    conn.close()
    assert names == ["s1", "s2", "s3", "s4", "s5"]


def test_old_package_verifiable_after_reimport_and_restart(client, monkeypatch):
    c, app_mod = client
    upload(c, R1, R2)
    payload = create_package(c)
    package_no = payload["package_no"]
    snap = _snapshot(app_mod, package_no)
    tag_code, sample_code = _orig_to_code(snap)
    valid_doc = {
        "package_no": package_no,
        "pairs": [
            {"sample_no": sample_code["s1"], "tag": tag_code["A"]},
            {"sample_no": sample_code["s2"], "tag": tag_code["B"]},
            {"sample_no": sample_code["s3"], "tag": tag_code["C"]},
        ],
    }
    # 重新导入一份完全不同的调查（整体替换 round1/round2/estimate）
    new_r1 = "tag\nX\nY\n"
    new_r2 = "sample_no,status,candidates\nz1,无标,\nz2,有标,X\n"
    assert upload(c, new_r1, new_r2).status_code == 302
    # 旧包仍留存且可按旧记录核验通过
    resp = verify_upload(c, valid_doc)
    assert resp.status_code == 200 and "核验通过" in resp.get_data(as_text=True)
    # 重新下载旧包内容不变
    resp = c.get(f"/recap/package/{package_no}")
    assert resp.status_code == 200
    assert json.loads(resp.get_data(as_text=True))["package_no"] == package_no
    # 模拟重启：同 DB 路径重新导入应用
    sys.modules.pop("app", None)
    app_mod2 = importlib.import_module("app")
    app_mod2.app.config["TESTING"] = True
    with app_mod2.app.test_client() as c2:
        resp = c2.post(
            "/recap/verify",
            data={"recap_file": (io.BytesIO(
                json.dumps(valid_doc, ensure_ascii=False).encode("utf-8")), "r.json")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200 and "核验通过" in resp.get_data(as_text=True)
        # 首页列出旧包
        assert package_no in c2.get("/").get_data(as_text=True)


def test_unclear_without_candidates_is_not_augment_root(client):
    # 看不清且无候选的样本不能作为增广起点；最大配对只覆盖有标样本
    c, app_mod = client
    r1 = "tag\nA\nB\n"
    r2 = (
        "sample_no,status,candidates\n"
        "s1,有标,A\n"
        "s2,看不清,\n"
        "s3,无标,\n"
    )
    upload(c, r1, r2)
    payload = create_package(c)
    snap = _snapshot(app_mod, payload["package_no"])
    assert snap["max_recap"] == 1
    tag_code, sample_code = _orig_to_code(snap)
    # 只交 s1-A（已达最大）→ 通过
    doc = {
        "package_no": payload["package_no"],
        "pairs": [{"sample_no": sample_code["s1"], "tag": tag_code["A"]}],
    }
    resp = verify_upload(c, doc)
    assert resp.status_code == 200 and "核验通过" in resp.get_data(as_text=True)
    # 空配对：可增配，步骤只补 s1-A（s2 无候选不会被当作起点）
    resp = verify_upload(c, {"package_no": payload["package_no"], "pairs": []})
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    panel = body.split("复算核验结论", 1)[1].split("本次认可的合法配对", 1)[0]
    assert "补上配对：样本 s1 — 标记 A" in panel and "s2" not in panel


def test_index_shows_package_section_after_upload(client):
    c, app_mod = client
    upload(c, R1, R2)
    create_package(c)
    body = c.get("/").get_data(as_text=True)
    assert "外部复算" in body and "RP-" in body and "重新下载该包" in body
    # 未发包前下载按钮处于禁用态
    sys.modules.pop("app", None)
    fresh = importlib.import_module("app")
    fresh.app.config["TESTING"] = True
    with fresh.app.test_client() as c2:
        body = c2.get("/").get_data(as_text=True)
        assert "下载当前调查的 JSON 复算包" in body
