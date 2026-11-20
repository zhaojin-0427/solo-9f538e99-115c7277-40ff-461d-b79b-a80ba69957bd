#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封闭池塘两轮抽样标记重捕估算系统（Flask + SQLite）。

规则：
- 首轮 CSV 登记放回鱼的唯一标记号（1~300 条）。
- 次轮 CSV 每行：唯一样本号、标记状态（有标/无标/看不清）、来自首轮的候选标记号。
- 配对：有标必配、无标不配、看不清可配可不配、每个首轮标记最多用一次。
- 求可行的最少/最多重捕数并各附一套配对（二分图匹配）。
- 鱼数估算 N = 首轮条数 × 次轮条数 ÷ 重捕数，重捕数为 0 的端点标为不可估。
- 次轮记录可按所见版号逐条更正（乐观锁）；过期、无解或写入失败时保留上次记录与估算。

外部复算：
- 现场标记号/样本号留在本站；下载的复算包只含包号与全部换过代号的两轮记录，
  次轮状态与候选关系照录。每次发包生成新代号，对应表与核验依据随包留存。
- 复算方交回含包号和配对清单的 JSON，上传后按发包时的记录核验：
  只收本包代号，核对有标必配、无标不配、候选关系、样本与标记各只用一次；
  任一合法的最大配对都算通过，合法但还能多配时给出增配一对的换配步骤，
  可定位的记录与换配步骤一律用原号显示。核验不改调查记录和估算。
"""

import csv
import io
import json
import os
import re
import secrets
import sqlite3
import sys
from collections import deque
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

sys.setrecursionlimit(10000)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "markrecap.db"))
MAX_ROWS = 300
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

PACKAGE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PACKAGE_PREFIX = "RP-"
TAG_CODE_PREFIX = "TAG-"
SAMPLE_CODE_PREFIX = "SMP-"

STATUSES = ("有标", "无标", "看不清")
STATUS_ALIASES = {
    "有标": "有标", "有標": "有标", "tagged": "有标", "y": "有标", "yes": "有标",
    "无标": "无标", "無標": "无标", "untagged": "无标", "n": "无标", "no": "无标",
    "看不清": "看不清", "unclear": "看不清", "unknown": "看不清", "?": "看不清", "未知": "看不清",
}
TAG_HEADERS = ("tag", "标记号", "标记", "tag_no", "mark", "编号")
SAMPLE_HEADERS = ("sample_no", "样本号", "sample", "id", "编号")
STATUS_HEADERS = ("status", "状态", "标记状态")
CAND_HEADERS = ("candidates", "候选号", "候选标记号", "候选", "cand")

SEPARATORS = re.compile(r"[;；|/、，,·\s]+")
FORBIDDEN_IN_TAG = re.compile(r"[;；|/、，,·\s]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS round1 (
    tag TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS round2 (
    sample_no  TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    candidates TEXT NOT NULL DEFAULT '[]',
    version    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS estimate (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    n1           INTEGER NOT NULL,
    n2           INTEGER NOT NULL,
    min_recap    INTEGER NOT NULL,
    max_recap    INTEGER NOT NULL,
    est_high     REAL,
    est_low      REAL,
    min_matching TEXT NOT NULL,
    max_matching TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recap_package (
    package_no TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    max_recap  INTEGER NOT NULL,
    n1         INTEGER NOT NULL,
    n2         INTEGER NOT NULL,
    snapshot   TEXT NOT NULL,
    file_json  TEXT NOT NULL
);
"""

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


class UploadError(Exception):
    """CSV 内容或更正内容校验失败。"""


# ---------------------------------------------------------------- 数据库

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def load_records(conn):
    return [
        {
            "sample_no": r["sample_no"],
            "status": r["status"],
            "candidates": json.loads(r["candidates"]),
            "version": r["version"],
        }
        for r in conn.execute(
            "SELECT sample_no, status, candidates, version FROM round2 ORDER BY rowid"
        )
    ]


def load_state():
    conn = get_db()
    try:
        tags = [r[0] for r in conn.execute("SELECT tag FROM round1 ORDER BY rowid")]
        records = load_records(conn)
        row = conn.execute("SELECT * FROM estimate WHERE id = 1").fetchone()
    finally:
        conn.close()
    est = None
    if row is not None:
        est = dict(row)
        est["min_matching"] = json.loads(est["min_matching"])
        est["max_matching"] = json.loads(est["max_matching"])
    return tags, records, est


def load_packages():
    """首页用：已留存复算包一览（不含大字段快照内容）。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT package_no, created_at, n1, n2, max_recap"
            " FROM recap_package ORDER BY rowid DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def save_estimate(conn, n1, n2, sol):
    est_high = None if sol["min_recap"] == 0 else n1 * n2 / sol["min_recap"]
    est_low = None if sol["max_recap"] == 0 else n1 * n2 / sol["max_recap"]
    conn.execute(
        "INSERT OR REPLACE INTO estimate"
        " (id, n1, n2, min_recap, max_recap, est_high, est_low,"
        "  min_matching, max_matching, updated_at)"
        " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            n1,
            n2,
            sol["min_recap"],
            sol["max_recap"],
            est_high,
            est_low,
            json.dumps(sol["min_matching"], ensure_ascii=False),
            json.dumps(sol["max_matching"], ensure_ascii=False),
            datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        ),
    )


# ---------------------------------------------------------------- 二分图匹配

def hopcroft_karp(adj, left_nodes, match_l=None, match_r=None):
    """Hopcroft-Karp 最大匹配。可传入已有匹配继续增广（用于第二阶段）。"""
    match_l = match_l if match_l is not None else {}
    match_r = match_r if match_r is not None else {}
    for u in left_nodes:
        match_l.setdefault(u, None)
    INF = float("inf")

    def bfs():
        dist = {}
        dq = deque()
        for u in left_nodes:
            if match_l[u] is None:
                dist[u] = 0
                dq.append(u)
            else:
                dist[u] = INF
        found_free = False
        while dq:
            u = dq.popleft()
            for v in adj.get(u, ()):
                w = match_r.get(v)
                if w is None:
                    found_free = True
                elif dist.get(w, INF) == INF:
                    dist[w] = dist[u] + 1
                    dq.append(w)
        return found_free, dist

    def dfs(u, dist):
        for v in adj.get(u, ()):
            w = match_r.get(v)
            if w is None or (dist.get(w, INF) == dist[u] + 1 and dfs(w, dist)):
                match_l[u] = v
                match_r[v] = u
                return True
        dist[u] = INF
        return False

    while True:
        found, dist = bfs()
        if not found:
            break
        for u in left_nodes:
            if match_l[u] is None:
                dfs(u, dist)
    return match_l, match_r


def hall_certificate(adj, left_nodes, match_l, match_r):
    """对最大匹配未覆盖全部有标样本的情形，给出霍尔定理证书：

    返回 (S, N)：S 为一组有标样本，N 为它们的候选号并集，且 |N| < |S|。
    """
    start = [u for u in left_nodes if match_l[u] is None]
    seen_l = set(start)
    seen_r = set()
    dq = deque(start)
    while dq:
        u = dq.popleft()
        for v in adj.get(u, ()):
            if match_l[u] == v or v in seen_r:
                continue
            seen_r.add(v)
            w = match_r.get(v)
            if w is not None and w not in seen_l:
                seen_l.add(w)
                dq.append(w)
    return sorted(seen_l), sorted(seen_r)


def compute_solution(records):
    """返回 dict：feasible；可行时含 min/max 重捕数与两套配对，否则含 hall 证书。"""
    tagged = [r for r in records if r["status"] == "有标"]
    unclear = [r for r in records if r["status"] == "看不清"]

    # 第一阶段：只要求覆盖全部有标样本（最少重捕 = 有标样本数）。
    adj_t = {r["sample_no"]: list(r["candidates"]) for r in tagged}
    left_t = [r["sample_no"] for r in tagged]
    match_l, match_r = hopcroft_karp(adj_t, left_t)
    if any(match_l[u] is None for u in left_t):
        s_set, n_set = hall_certificate(adj_t, left_t, match_l, match_r)
        return {
            "feasible": False,
            "hall": {
                "samples": [
                    {"sample_no": u, "candidates": adj_t[u]} for u in s_set
                ],
                "union": n_set,
                "sample_count": len(s_set),
                "union_count": len(n_set),
            },
        }

    min_pairs = {u: match_l[u] for u in left_t}

    # 第二阶段：在保留全部有标配对的前提下，让看不清样本尽量增广（最多重捕）。
    full_adj = dict(adj_t)
    left_all = list(left_t)
    for r in unclear:
        full_adj[r["sample_no"]] = list(r["candidates"])
        left_all.append(r["sample_no"])
    match_l2, _ = hopcroft_karp(full_adj, left_all, match_l, match_r)

    min_matching = [
        {"sample_no": u, "tag": min_pairs[u]} for u in sorted(min_pairs)
    ]
    max_matching = [
        {"sample_no": u, "tag": match_l2[u]}
        for u in sorted(left_all)
        if match_l2[u] is not None
    ]
    return {
        "feasible": True,
        "min_recap": len(left_t),
        "max_recap": len(max_matching),
        "min_matching": min_matching,
        "max_matching": max_matching,
    }


# ------------------------------------------------ 外部复算包（换代号 / 留存 / 核验）

def _new_package_no(conn):
    """每次发包使用全新代号；库内已存在时重新抽取。"""
    while True:
        suffix = "".join(secrets.choice(PACKAGE_ALPHABET) for _ in range(8))
        no = PACKAGE_PREFIX + suffix
        exists = conn.execute(
            "SELECT 1 FROM recap_package WHERE package_no = ?", (no,)
        ).fetchone()
        if exists is None:
            return no


def build_recap_package(package_no, tags, records, max_recap):
    """构造对外复算包与本站留存快照。

    对外文件只含包号和换过代号的两轮记录：首轮仅代号，次轮记录的状态与候选关系照录，
    但标记号、样本号全部换成包内代号；代号次序用密码学随机源打乱，不沿用本站次序。
    留存快照保存代号↔原号对应表及核验依据（原始两轮记录与最大重捕数）。
    """
    shuffled_tags = list(tags)
    secrets.SystemRandom().shuffle(shuffled_tags)
    nonce = package_no.split("-", 1)[1][:4]
    tag_prefix = f"{TAG_CODE_PREFIX}{nonce}-"
    width = max(3, len(str(len(tags))))
    tag_map = {
        orig: f"{tag_prefix}{i:0{width}d}"
        for i, orig in enumerate(shuffled_tags, start=1)
    }

    shuffled_records = list(records)
    secrets.SystemRandom().shuffle(shuffled_records)
    sample_prefix = f"{SAMPLE_CODE_PREFIX}{nonce}-"
    width_s = max(3, len(str(len(records))))
    sample_map = {
        r["sample_no"]: f"{sample_prefix}{i:0{width_s}d}"
        for i, r in enumerate(shuffled_records, start=1)
    }

    round1_payload = [{"tag": tag_map[t]} for t in shuffled_tags]
    round2_payload = [
        {
            "sample_no": sample_map[r["sample_no"]],
            "status": r["status"],
            "candidates": [tag_map[c] for c in r["candidates"]],
        }
        for r in shuffled_records
    ]
    payload = {
        "package_no": package_no,
        "round1": round1_payload,
        "round2": round2_payload,
    }
    snapshot = {
        "package_no": package_no,
        "max_recap": max_recap,
        "tag_map": tag_map,
        "sample_map": sample_map,
        "round1": list(tags),
        "round2": [
            {
                "sample_no": r["sample_no"],
                "status": r["status"],
                "candidates": list(r["candidates"]),
            }
            for r in records
        ],
    }
    return payload, snapshot


def _find_augment_step(pairs, status_by_sample, candidates_by_sample, tagset):
    """在合法配对 pairs 上找一条增广路，返回增配一对所需的换配步骤。

    返回 {"add": [(sample, tag)], "remove": [(sample, tag)]}，均为原号；
    已是最大配对、无法再增时返回 None。
    """
    used_samples = {s for s, _ in pairs}
    used_tags = {t for _, t in pairs}
    adj = {
        s: [t for t in cands if t in tagset]
        for s, cands in candidates_by_sample.items()
    }
    match_t = {t: s for s, t in pairs}

    # 只允许“有标 / 看不清”的未配对样本作为增广起点（无标不配）。
    roots = [
        s
        for s, st in status_by_sample.items()
        if st in ("有标", "看不清")
        and s not in used_samples
        and adj.get(s)
    ]
    # parent 记录每个到达节点的前驱；("s", 起点样本) 的前驱为 None。
    parent = {}
    dq = deque()
    for s in roots:
        parent[("s", s)] = None
        dq.append(("s", s))
    free_tag = None
    while dq and free_tag is None:
        kind, node = dq.popleft()
        if kind != "s":
            continue
        for t in adj.get(node, ()):
            t_key = ("t", t)
            if t_key in parent:
                continue
            parent[t_key] = ("s", node)
            if t not in used_tags:
                free_tag = t
                break
            owner = match_t[t]  # 该标记当前配给的样本，沿配对边继续走
            s_key = ("s", owner)
            if s_key not in parent:
                parent[s_key] = t_key
                dq.append(s_key)
    if free_tag is None:
        return None

    # 回溯：free_tag → 样本 →（该样本原占用的）标记 → … → 起点样本
    add, remove = [], []
    cur = ("t", free_tag)
    while cur is not None:
        tag_node = cur[1]
        owner_key = parent[cur]          # ("s", 经此样本连到 tag_node)
        sample = owner_key[1]
        add.append((sample, tag_node))
        prev_tag_key = parent.get(owner_key)
        if prev_tag_key is None:
            cur = None                  # 到达起点样本
        else:
            remove.append((sample, prev_tag_key[1]))
            cur = prev_tag_key
    add.reverse()
    remove.reverse()
    return {"add": add, "remove": remove}


def verify_recap_result(doc, snapshot):
    """按发包时留存的记录核验复算方交回的配对清单。

    只收本包代号；可定位的记录与换配步骤一律换算回原号。
    返回 dict：passed / max_recap / recap_count / reasons[] / augment / pairs。
    """
    tag_map = snapshot["tag_map"]
    sample_map = snapshot["sample_map"]
    inv_tag = {v: k for k, v in tag_map.items()}
    inv_sample = {v: k for k, v in sample_map.items()}
    records = snapshot["round2"]
    status_by_code = {sample_map[r["sample_no"]]: r["status"] for r in records}
    cands_by_code = {
        sample_map[r["sample_no"]]: [tag_map[c] for c in r["candidates"]]
        for r in records
    }
    max_recap = snapshot["max_recap"]

    reasons = []
    malformed = []
    for idx, item in enumerate(doc.get("pairs", []), start=1):
        if not isinstance(item, dict):
            malformed.append(f"配对清单第 {idx} 项不是对象。")
            continue
        s_code = item.get("sample_no")
        t_code = item.get("tag")
        if not isinstance(s_code, str) or not isinstance(t_code, str):
            malformed.append(
                f"配对清单第 {idx} 项缺少字符串字段 sample_no/tag。"
            )
    if malformed:
        reasons.extend(malformed[:10])

    seen_samples, seen_tags = set(), set()
    dup_samples, dup_tags = [], []
    valid_pairs, pair_reasons = [], []
    for item in doc.get("pairs", []):
        if not isinstance(item, dict):
            continue
        s_code, t_code = item.get("sample_no"), item.get("tag")
        if not isinstance(s_code, str) or not isinstance(t_code, str):
            continue
        s_orig = inv_sample.get(s_code)
        t_orig = inv_tag.get(t_code)
        if s_orig is None:
            pair_reasons.append(f"样本代号 {s_code} 不属于本包（只收本包代号）。")
            continue
        if t_orig is None:
            pair_reasons.append(
                f"标记代号 {t_code} 不属于本包（只收本包代号）；"
                f"出现在样本 {s_orig} 的配对中。"
            )
            continue
        # 重复使用先单独记录，保证“各只用一次”的核对不被其它规则掩盖
        if s_code in seen_samples:
            dup_samples.append(s_orig)
            continue
        if t_code in seen_tags:
            dup_tags.append(t_orig)
            continue
        seen_samples.add(s_code)
        seen_tags.add(t_code)

        status = status_by_code[s_code]
        if status == "无标":
            pair_reasons.append(
                f"无标样本 {s_orig} 不应配对，却配了标记 {t_orig}（无标不配）。"
            )
            continue
        if t_code not in cands_by_code[s_code]:
            cands_orig = [
                inv_tag[c] for c in cands_by_code[s_code]
            ]
            cand_text = "、".join(cands_orig) if cands_orig else "（无候选号）"
            pair_reasons.append(
                f"样本 {s_orig}（{status}）与标记 {t_orig} 不在候选关系内；"
                f"其候选为：{cand_text}。"
            )
            continue
        valid_pairs.append((s_orig, t_orig))

    if dup_samples:
        uniq = sorted(set(dup_samples))
        pair_reasons.append(
            "同一样本被使用多次，涉及样本："
            + "、".join(uniq[:10])
            + (" 等。" if len(uniq) > 10 else "。")
        )
    if dup_tags:
        uniq = sorted(set(dup_tags))
        pair_reasons.append(
            "同一标记被使用多次，涉及标记："
            + "、".join(uniq[:10])
            + (" 等。" if len(uniq) > 10 else "。")
        )

    reasons.extend(pair_reasons)

    result = {
        "passed": False,
        "max_recap": max_recap,
        "recap_count": len(valid_pairs),
        "reasons": reasons,
        "augment": None,
        "pairs": sorted(valid_pairs),
        "legal": not reasons,
    }
    if reasons:
        return result

    # 无冲突、满足无标不配与候选关系的配对都可在可行实例上继续增广：
    # 有标样本未配全等情形统一归入“合法但还能多配”，由增广路步骤指出如何补齐。
    if len(valid_pairs) >= max_recap:
        result["passed"] = True
        return result

    status_by_sample = {r["sample_no"]: r["status"] for r in records}
    cands_by_sample = {r["sample_no"]: r["candidates"] for r in records}
    step = _find_augment_step(
        valid_pairs, status_by_sample, cands_by_sample, set(snapshot["round1"])
    )
    if step is None:
        # 理论上不应到达：合法配对数低于本站已算出的最大重捕数必有增广路。
        result["reasons"].append(
            f"当前合法配对 {len(valid_pairs)} 对，少于发包时的最大重捕数 "
            f"{max_recap}，但未能在本站记录上找到增广路，请联系本站核查留存数据。"
        )
        result["legal"] = False
        return result
    result["augment"] = step
    return result


# ---------------------------------------------------------------- CSV 解析

def read_text_upload(fs):
    data = fs.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("文件过大（上限 2MB）。")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadError("文件编码需为 UTF-8。")


def _rows(text):
    reader = csv.reader(io.StringIO(text))
    return [row for row in reader if any(cell.strip() for cell in row)]


def _find_col(header, names):
    lowered = [h.strip().lower() for h in header]
    for name in names:
        if name.lower() in lowered:
            return lowered.index(name.lower())
    return None


def parse_round1(text):
    rows = _rows(text)
    if not rows:
        raise UploadError("首轮 CSV 为空。")
    idx = _find_col(rows[0], TAG_HEADERS)
    if idx is None:
        raise UploadError("首轮 CSV 表头需包含 tag（或：标记号 / 标记 / 编号）。")
    tags = []
    for lineno, row in enumerate(rows[1:], start=2):
        if idx >= len(row) or not row[idx].strip():
            raise UploadError(f"首轮第 {lineno} 行缺少标记号。")
        tag = row[idx].strip()
        if FORBIDDEN_IN_TAG.search(tag):
            raise UploadError(
                f"首轮第 {lineno} 行标记号 {tag!r} 含非法字符（空格或 ;；|/、，,）。"
            )
        tags.append(tag)
    if not 1 <= len(tags) <= MAX_ROWS:
        raise UploadError(f"首轮条数须为 1~{MAX_ROWS}，实际 {len(tags)} 条。")
    seen, dups = set(), set()
    for t in tags:
        if t in seen:
            dups.add(t)
        seen.add(t)
    if dups:
        sample = "、".join(sorted(dups)[:5])
        raise UploadError(f"首轮标记号不唯一：{sample}{' 等' if len(dups) > 5 else ''}。")
    return tags


def normalize_status(raw, lineno=None):
    text = raw.strip()
    if text in STATUS_ALIASES:
        return STATUS_ALIASES[text]
    if text.lower() in STATUS_ALIASES:
        return STATUS_ALIASES[text.lower()]
    where = f"第 {lineno} 行" if lineno else "该记录"
    raise UploadError(
        f"次轮{where}状态 {raw!r} 无法识别，应为：有标 / 无标 / 看不清。"
    )


def validate_candidates(raw, tagset, status, lineno=None):
    cands = []
    for tok in SEPARATORS.split(raw.strip()):
        if tok and tok not in cands:
            cands.append(tok)
    where = f"第 {lineno} 行" if lineno else "该记录"
    unknown = [c for c in cands if c not in tagset]
    if unknown:
        raise UploadError(
            f"次轮{where}候选号 {'、'.join(unknown[:5])} 不在首轮标记号中。"
        )
    if status == "无标" and cands:
        raise UploadError(f"次轮{where}状态为无标，按规则不应填写候选号。")
    return cands


def parse_round2(text, tagset):
    rows = _rows(text)
    if not rows:
        raise UploadError("次轮 CSV 为空。")
    header = rows[0]
    i_no = _find_col(header, SAMPLE_HEADERS)
    i_st = _find_col(header, STATUS_HEADERS)
    i_ca = _find_col(header, CAND_HEADERS)
    if i_no is None or i_st is None or i_ca is None:
        raise UploadError(
            "次轮 CSV 表头需包含 sample_no,status,candidates"
            "（或：样本号 / 状态 / 候选号）。"
        )
    records = []
    seen = set()
    for lineno, row in enumerate(rows[1:], start=2):
        def cell(i):
            return row[i].strip() if i < len(row) else ""

        sample_no = cell(i_no)
        if not sample_no:
            raise UploadError(f"次轮第 {lineno} 行缺少样本号。")
        if sample_no in seen:
            raise UploadError(f"次轮样本号重复：{sample_no}。")
        seen.add(sample_no)
        status = normalize_status(cell(i_st), lineno)
        cands = validate_candidates(cell(i_ca), tagset, status, lineno)
        records.append(
            {"sample_no": sample_no, "status": status, "candidates": cands}
        )
    if not 1 <= len(records) <= MAX_ROWS:
        raise UploadError(f"次轮条数须为 1~{MAX_ROWS}，实际 {len(records)} 条。")
    return records


# ---------------------------------------------------------------- 页面

def render_index(**extra):
    tags, records, est = load_state()
    packages = load_packages()
    return render_template(
        "index.html",
        tags=tags,
        records=records,
        est=est,
        packages=packages,
        statuses=STATUSES,
        **extra,
    )


@app.route("/")
def index():
    return render_index()


@app.route("/upload", methods=["POST"])
def upload():
    try:
        f1 = request.files.get("round1_file")
        f2 = request.files.get("round2_file")
        if f1 is None or f2 is None or not f1.filename or not f2.filename:
            raise UploadError("请同时选择首轮与次轮两个 CSV 文件。")
        tags = parse_round1(read_text_upload(f1))
        records = parse_round2(read_text_upload(f2), set(tags))
        sol = compute_solution(records)
    except UploadError as exc:
        return render_index(error=str(exc)), 400

    if not sol["feasible"]:
        return (
            render_index(
                error="次轮数据无可行配对，本次上传未保存，仍保留上次记录与估算。",
                hall=sol["hall"],
            ),
            422,
        )

    try:
        conn = get_db()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM round1")
            conn.execute("DELETE FROM round2")
            conn.executemany(
                "INSERT INTO round1(tag) VALUES (?)", [(t,) for t in tags]
            )
            conn.executemany(
                "INSERT INTO round2(sample_no, status, candidates, version)"
                " VALUES (?, ?, ?, 1)",
                [
                    (
                        r["sample_no"],
                        r["status"],
                        json.dumps(r["candidates"], ensure_ascii=False),
                    )
                    for r in records
                ],
            )
            save_estimate(conn, len(tags), len(records), sol)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except sqlite3.Error as exc:
        app.logger.exception("上传写入失败")
        return render_index(error=f"写入失败（{exc}），已保留上次记录与估算。"), 500

    flash("上传成功，已保存新估算。", "ok")
    return redirect(url_for("index"))


@app.route("/correct", methods=["POST"])
def correct():
    sample_no = (request.form.get("sample_no") or "").strip()
    version_seen = (request.form.get("version") or "").strip()
    status_raw = request.form.get("status") or ""
    cand_raw = request.form.get("candidates") or ""

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM round2 WHERE sample_no = ?", (sample_no,)
        ).fetchone()
        if row is None:
            return (
                render_index(
                    error=f"次轮记录 {sample_no!r} 不存在（可能已重新上传），"
                    "请刷新后重试；已保留上次记录与估算。"
                ),
                409,
            )
        if str(row["version"]) != version_seen:
            return (
                render_index(
                    error=f"记录 {sample_no} 已过期：所见版号为 "
                    f"{version_seen or '（空）'}，当前版号为 {row['version']}。"
                    "本次更正未保存，已保留上次记录与估算。"
                ),
                409,
            )
        try:
            status = normalize_status(status_raw)
            tags = [r[0] for r in conn.execute("SELECT tag FROM round1")]
            cands = validate_candidates(cand_raw, set(tags), status)
        except UploadError as exc:
            return render_index(error=str(exc)), 400

        records = load_records(conn)
        for r in records:
            if r["sample_no"] == sample_no:
                r["status"] = status
                r["candidates"] = cands
        sol = compute_solution(records)
        if not sol["feasible"]:
            return (
                render_index(
                    error="更正后无可行配对，本次更正未保存，仍保留上次记录与估算。",
                    hall=sol["hall"],
                ),
                422,
            )

        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                "UPDATE round2 SET status = ?, candidates = ?,"
                " version = version + 1"
                " WHERE sample_no = ? AND version = ?",
                (
                    status,
                    json.dumps(cands, ensure_ascii=False),
                    sample_no,
                    row["version"],
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return (
                    render_index(
                        error=f"记录 {sample_no} 已过期（并发修改），"
                        "本次更正未保存，已保留上次记录与估算。"
                    ),
                    409,
                )
            save_estimate(conn, len(tags), len(records), sol)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
    except sqlite3.Error as exc:
        app.logger.exception("更正写入失败")
        return render_index(error=f"写入失败（{exc}），已保留上次记录与估算。"), 500
    finally:
        conn.close()

    flash(
        f"记录 {sample_no} 更正成功（新版号 {row['version'] + 1}），已保存新估算。",
        "ok",
    )
    return redirect(url_for("index"))


# ------------------------------------------------------------ 外部复算包路由

def _package_response(package_no, file_json):
    filename = f"recap_package_{package_no}.json"
    return Response(
        file_json,
        mimetype="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@app.route("/recap/package", methods=["POST"])
def recap_create_package():
    """把当前调查打包为 JSON 复算包：留存成功才给下载，且文件与留存取自同一状态。"""
    conn = get_db()
    try:
        try:
            conn.execute("BEGIN")
            tags = [r[0] for r in conn.execute("SELECT tag FROM round1 ORDER BY rowid")]
            records = load_records(conn)
            row = conn.execute(
                "SELECT max_recap FROM estimate WHERE id = 1"
            ).fetchone()
            if not tags or not records or row is None:
                conn.rollback()
                return (
                    render_index(
                        error="尚无已提交的调查与估算，无法生成复算包，请先上传两轮 CSV。"
                    ),
                    400,
                )
            package_no = _new_package_no(conn)
            payload, snapshot = build_recap_package(
                package_no, tags, records, row["max_recap"]
            )
            file_json = json.dumps(payload, ensure_ascii=False, indent=2)
            conn.execute(
                "INSERT INTO recap_package"
                " (package_no, created_at, max_recap, n1, n2, snapshot, file_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    package_no,
                    datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    snapshot["max_recap"],
                    len(tags),
                    len(records),
                    json.dumps(snapshot, ensure_ascii=False),
                    file_json,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    except sqlite3.Error as exc:
        app.logger.exception("复算包留存失败")
        return (
            render_index(error=f"复算包留存失败（{exc}），本次未提供下载，调查记录未改动。"),
            500,
        )
    finally:
        conn.close()

    flash(f"复算包 {package_no} 已留存并开始下载；现场标记号与样本号未出现在包内。", "ok")
    return _package_response(package_no, file_json)


@app.route("/recap/package/<package_no>")
def recap_download_package(package_no):
    """重新下载已留存的复算包（内容与首次下载完全一致）。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT file_json FROM recap_package WHERE package_no = ?",
            (package_no,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return render_index(error=f"复算包 {package_no} 不存在或未在本站留存。"), 404
    return _package_response(package_no, row["file_json"])


@app.route("/recap/verify", methods=["POST"])
def recap_verify():
    """上传复算方交回的 JSON（含包号与配对清单），当次给出核验结论。"""
    fs = request.files.get("recap_file")
    if fs is None or not fs.filename:
        return render_index(error="请选择复算方交回的 JSON 文件。"), 400
    data = fs.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return render_index(error="文件过大（上限 2MB）。"), 400
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return render_index(error=f"交回文件不是合法的 UTF-8 JSON：{exc}"), 400
    if not isinstance(doc, dict):
        return render_index(error="交回 JSON 顶层须为对象，包含 package_no 与 pairs。"), 400

    package_no = doc.get("package_no")
    if not isinstance(package_no, str):
        return render_index(error="交回 JSON 缺少字符串字段 package_no。"), 400
    if not isinstance(doc.get("pairs"), list):
        return (
            render_index(
                error="交回 JSON 的 pairs 须为数组，每项含 sample_no 与 tag。"
            ),
            400,
        )

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT snapshot FROM recap_package WHERE package_no = ?",
            (package_no,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return (
            render_index(
                error=f"包号 {package_no} 未在本站留存，只收本站发出且留存的包代号。"
            ),
            404,
        )

    snapshot = json.loads(row["snapshot"])
    result = verify_recap_result(doc, snapshot)
    status = 200 if result["passed"] else 422
    return render_index(recap_result=result, package_no=package_no), status


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
