"""HTTP 层：下单鉴权、渠道未配置报错、回调验签三大红线（伪造签名/篡改金额/
重复回调）、查单归属隔离。

按派单要求，三条安全判据各自都有对应用例：
- ``test_wechat_notify_rejects_forged_signature`` / ``test_alipay_notify_rejects_forged_signature``
- ``test_wechat_notify_rejects_amount_mismatch`` / ``test_alipay_notify_rejects_amount_mismatch``
- ``test_wechat_notify_duplicate_only_delivers_once`` / ``test_alipay_notify_duplicate_only_delivers_once``
"""
from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import db, quota_addon
from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app
from app.payments import alipay as alipay_mod
from app.payments.crypto_rsa import sign_pkcs1v15_sha256
from tests.payments_pki_helpers import (
    aes256_gcm_encrypt,
    encode_fake_certificate_pem,
    encode_pkcs8_private_key_pem,
    encode_spki_pem,
    generate_rsa_keypair,
)

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}
_API_V3_KEY = "0123456789abcdef0123456789abcdef"  # 恰好 32 字节


def _make_user() -> str:
    conn = get_conn()
    user_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier
           ) VALUES(?,?,?,?,'local','active',0,0,?,'free')""",
        (user_id, f"buyer-{user_id}", "测试买家", hash_password("pw-test-000000"), now()),
    )
    conn.commit()
    return user_id


def _auth_headers(user_id: str) -> dict[str, str]:
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


def _verify_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def wechat_env(monkeypatch, tmp_path):
    platform_keys = generate_rsa_keypair(1024)
    merchant_keys = generate_rsa_keypair(1024)
    n_p, e_p, d_p, _, _ = platform_keys
    n_m, e_m, d_m, p_m, q_m = merchant_keys
    (tmp_path / "wx_platform.pem").write_text(encode_fake_certificate_pem(n_p, e_p))
    (tmp_path / "wx_merchant.pem").write_text(encode_pkcs8_private_key_pem(n_m, e_m, d_m, p_m, q_m))
    monkeypatch.setenv("WECHAT_PAY_APP_ID", "wx-test-app")
    monkeypatch.setenv("WECHAT_PAY_MCH_ID", "wx-test-mch")
    monkeypatch.setenv("WECHAT_PAY_API_V3_KEY", _API_V3_KEY)
    monkeypatch.setenv("WECHAT_PAY_SERIAL_NO", "wx-test-serial")
    monkeypatch.setenv("WECHAT_PAY_PRIVATE_KEY_PATH", str(tmp_path / "wx_merchant.pem"))
    monkeypatch.setenv("WECHAT_PAY_PLATFORM_CERT_PATH", str(tmp_path / "wx_platform.pem"))
    monkeypatch.setenv("PAYMENTS_PUBLIC_BASE_URL", "https://test.example.com")
    return {"platform_keys": platform_keys}


@pytest.fixture()
def alipay_env(monkeypatch, tmp_path):
    alipay_keys = generate_rsa_keypair(1024)  # 支付宝侧密钥：签通知、我方用其公钥验
    merchant_keys = generate_rsa_keypair(1024)  # 我方私钥：签我方发起的请求
    n_a, e_a, d_a, _, _ = alipay_keys
    n_m, e_m, d_m, p_m, q_m = merchant_keys
    (tmp_path / "alipay_public.pem").write_text(encode_spki_pem(n_a, e_a))
    (tmp_path / "alipay_merchant.pem").write_text(encode_pkcs8_private_key_pem(n_m, e_m, d_m, p_m, q_m))
    monkeypatch.setenv("ALIPAY_APP_ID", "alipay-test-app")
    monkeypatch.setenv("ALIPAY_PRIVATE_KEY_PATH", str(tmp_path / "alipay_merchant.pem"))
    monkeypatch.setenv("ALIPAY_PUBLIC_KEY_PATH", str(tmp_path / "alipay_public.pem"))
    monkeypatch.setenv("PAYMENTS_PUBLIC_BASE_URL", "https://test.example.com")
    return {"alipay_keys": alipay_keys}


def _build_wechat_notify_body(*, out_trade_no: str, amount_total: int, trade_state: str = "SUCCESS") -> bytes:
    plaintext = json.dumps(
        {"out_trade_no": out_trade_no, "transaction_id": f"wxtxn-{out_trade_no}",
         "trade_state": trade_state, "amount": {"total": amount_total}},
        ensure_ascii=False,
    ).encode("utf-8")
    nonce_str = uuid.uuid4().hex[:12]
    aad_str = "transaction"
    ct_and_tag = aes256_gcm_encrypt(_API_V3_KEY.encode("utf-8"), nonce_str.encode("utf-8"), plaintext, aad_str.encode("utf-8"))
    resource = {
        "original_type": "transaction", "algorithm": "AEAD_AES_256_GCM",
        "ciphertext": base64.b64encode(ct_and_tag).decode("ascii"),
        "associated_data": aad_str, "nonce": nonce_str,
    }
    envelope = {"id": "evt-1", "event_type": "TRANSACTION.SUCCESS", "resource_type": "encrypt-resource", "resource": resource}
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sign_wechat_headers(raw_body: bytes, *, n: int, d: int) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = f"{timestamp}\n{nonce}\n{raw_body.decode('utf-8')}\n".encode("utf-8")
    signature = base64.b64encode(sign_pkcs1v15_sha256(message, n, d)).decode("ascii")
    return {"Wechatpay-Timestamp": timestamp, "Wechatpay-Nonce": nonce, "Wechatpay-Signature": signature}


def _build_alipay_form(*, out_trade_no: str, total_amount: str, trade_status: str, n: int, d: int) -> dict[str, str]:
    fields = {
        "app_id": "alipay-test-app", "out_trade_no": out_trade_no, "trade_no": f"aptxn-{out_trade_no}",
        "trade_status": trade_status, "total_amount": total_amount, "sign_type": "RSA2",
    }
    signature = sign_pkcs1v15_sha256(alipay_mod._sign_string(fields).encode("utf-8"), n, d)
    return {**fields, "sign": base64.b64encode(signature).decode("ascii")}


# ---------------------------------------------------------------------------
# 下单：鉴权 + 未配置报错
# ---------------------------------------------------------------------------

def test_create_order_requires_authentication():
    client = TestClient(app)
    resp = client.post("/api/payments/orders", headers=_HEADERS, json={"channel": "wechat", "product": "video_addon", "packages": 1})
    assert resp.status_code == 401


def test_create_order_reports_missing_config_clearly():
    client = TestClient(app)
    uid = _make_user()
    resp = client.post(
        "/api/payments/orders", headers=_auth_headers(uid),
        json={"channel": "wechat", "product": "video_addon", "packages": 1},
    )
    assert resp.status_code == 503
    assert "WECHAT_PAY" in resp.text  # 明确指出去哪配，不是一句"服务不可用"


def test_create_order_rejects_invalid_product_params():
    client = TestClient(app)
    uid = _make_user()
    resp = client.post(
        "/api/payments/orders", headers=_auth_headers(uid),
        json={"channel": "wechat", "product": "tier_upgrade", "target_tier": "free"},
    )
    assert resp.status_code == 422  # free 不是可购买的升级目标


def test_create_order_builds_alipay_redirect_url_without_network(alipay_env):
    """支付宝下单不需要出站网络调用，可以端到端真的跑一遍（微信 Native 需要
    真实 HTTP 调用微信服务器，本环境没有商户号做不到，见验收报告）。"""
    client = TestClient(app)
    uid = _make_user()
    resp = client.post(
        "/api/payments/orders", headers=_auth_headers(uid),
        json={"channel": "alipay", "product": "video_addon", "packages": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amount_fen"] == round(quota_addon.ADDON_PACKAGE_PRICE_CNY * 100)
    assert "redirect_url" in body["pay_params"]
    assert "alipay.trade.page.pay" in body["pay_params"]["redirect_url"]
    order = get_conn().execute("SELECT status FROM payment_orders WHERE id=?", (body["order_id"],)).fetchone()
    assert order["status"] == "pending"


# ---------------------------------------------------------------------------
# 查单：只能看自己的订单
# ---------------------------------------------------------------------------

def test_get_order_is_scoped_to_owner(alipay_env):
    client = TestClient(app)
    uid_a = _make_user()
    uid_b = _make_user()
    create_resp = client.post(
        "/api/payments/orders", headers=_auth_headers(uid_a),
        json={"channel": "alipay", "product": "video_addon", "packages": 1},
    )
    order_id = create_resp.json()["order_id"]

    own = client.get(f"/api/payments/orders/{order_id}", headers=_auth_headers(uid_a))
    assert own.status_code == 200

    other = client.get(f"/api/payments/orders/{order_id}", headers=_auth_headers(uid_b))
    assert other.status_code == 404  # 不能靠猜订单号看别人的订单


# ---------------------------------------------------------------------------
# 微信回调：伪造签名 / 篡改金额 / 重复回调
# ---------------------------------------------------------------------------

def test_wechat_notify_rejects_forged_signature(wechat_env):
    client = TestClient(app)
    uid = _make_user()
    order_id = new_id("pay")
    from app.payments import orders
    orders.create_order(
        get_conn(), order_id=order_id, user_id=uid, channel="wechat", product="video_addon",
        product_detail={"packages": 1}, amount_fen=19900, created_at=now(),
    )
    get_conn().commit()

    body = _build_wechat_notify_body(out_trade_no=order_id, amount_total=19900)
    forged_keys = generate_rsa_keypair(1024)  # 用一把跟平台证书无关的密钥签名
    headers = _sign_wechat_headers(body, n=forged_keys[0], d=forged_keys[2])
    resp = client.post("/api/payments/notify/wechat", headers=headers, content=body)
    assert resp.status_code != 200

    verify = _verify_conn()
    row = verify.execute("SELECT status FROM payment_orders WHERE id=?", (order_id,)).fetchone()
    assert row["status"] == "pending"  # 伪造签名：订单完全没被动过
    verify.close()


def test_wechat_notify_rejects_amount_mismatch(wechat_env):
    client = TestClient(app)
    uid = _make_user()
    order_id = new_id("pay")
    from app.payments import orders
    orders.create_order(
        get_conn(), order_id=order_id, user_id=uid, channel="wechat", product="video_addon",
        product_detail={"packages": 1}, amount_fen=19900, created_at=now(),
    )
    get_conn().commit()

    # 签名/加密全部合法（用真的平台私钥签），但通知里的金额跟订单不符。
    body = _build_wechat_notify_body(out_trade_no=order_id, amount_total=100)
    n_p, _, d_p, _, _ = wechat_env["platform_keys"]
    headers = _sign_wechat_headers(body, n=n_p, d=d_p)
    resp = client.post("/api/payments/notify/wechat", headers=headers, content=body)
    assert resp.status_code != 200

    verify = _verify_conn()
    row = verify.execute("SELECT status FROM payment_orders WHERE id=?", (order_id,)).fetchone()
    assert row["status"] == "pending"  # 金额不符：绝不发货
    verify.close()


def test_wechat_notify_duplicate_only_delivers_once(wechat_env):
    client = TestClient(app)
    uid = _make_user()
    order_id = new_id("pay")
    from app.payments import orders
    orders.create_order(
        get_conn(), order_id=order_id, user_id=uid, channel="wechat", product="video_addon",
        product_detail={"packages": 1}, amount_fen=19900, created_at=now(),
    )
    get_conn().commit()

    body = _build_wechat_notify_body(out_trade_no=order_id, amount_total=19900)
    n_p, _, d_p, _, _ = wechat_env["platform_keys"]
    headers = _sign_wechat_headers(body, n=n_p, d=d_p)

    resp1 = client.post("/api/payments/notify/wechat", headers=headers, content=body)
    assert resp1.status_code == 200, resp1.text
    resp2 = client.post("/api/payments/notify/wechat", headers=headers, content=body)  # 渠道重试，原样重放
    assert resp2.status_code == 200, resp2.text

    verify = _verify_conn()
    grants = verify.execute(
        "SELECT COUNT(*) c FROM quota_ledger WHERE resource='video_addon_seconds' AND attempt_key=? AND reason='grant'",
        (order_id,),
    ).fetchone()
    assert grants["c"] == 1
    verify.close()


# ---------------------------------------------------------------------------
# 支付宝回调：伪造签名 / 篡改金额 / 重复回调
# ---------------------------------------------------------------------------

def test_alipay_notify_rejects_forged_signature(alipay_env):
    client = TestClient(app)
    uid = _make_user()
    order_id = new_id("pay")
    from app.payments import orders
    orders.create_order(
        get_conn(), order_id=order_id, user_id=uid, channel="alipay", product="video_addon",
        product_detail={"packages": 1}, amount_fen=19900, created_at=now(),
    )
    get_conn().commit()

    forged_keys = generate_rsa_keypair(1024)
    form = _build_alipay_form(
        out_trade_no=order_id, total_amount="199.00", trade_status="TRADE_SUCCESS",
        n=forged_keys[0], d=forged_keys[2],
    )
    resp = client.post("/api/payments/notify/alipay", headers=_HEADERS, data=form)
    assert resp.text.strip() != "success"

    verify = _verify_conn()
    row = verify.execute("SELECT status FROM payment_orders WHERE id=?", (order_id,)).fetchone()
    assert row["status"] == "pending"
    verify.close()


def test_alipay_notify_rejects_amount_mismatch(alipay_env):
    client = TestClient(app)
    uid = _make_user()
    order_id = new_id("pay")
    from app.payments import orders
    orders.create_order(
        get_conn(), order_id=order_id, user_id=uid, channel="alipay", product="video_addon",
        product_detail={"packages": 1}, amount_fen=19900, created_at=now(),
    )
    get_conn().commit()

    n_a, _, d_a, _, _ = alipay_env["alipay_keys"]
    form = _build_alipay_form(
        out_trade_no=order_id, total_amount="1.00", trade_status="TRADE_SUCCESS", n=n_a, d=d_a,
    )
    resp = client.post("/api/payments/notify/alipay", headers=_HEADERS, data=form)
    assert resp.text.strip() != "success"

    verify = _verify_conn()
    row = verify.execute("SELECT status FROM payment_orders WHERE id=?", (order_id,)).fetchone()
    assert row["status"] == "pending"
    verify.close()


def test_alipay_notify_duplicate_only_delivers_once(alipay_env):
    client = TestClient(app)
    uid = _make_user()
    order_id = new_id("pay")
    from app.payments import orders
    orders.create_order(
        get_conn(), order_id=order_id, user_id=uid, channel="alipay", product="video_addon",
        product_detail={"packages": 1}, amount_fen=19900, created_at=now(),
    )
    get_conn().commit()

    n_a, _, d_a, _, _ = alipay_env["alipay_keys"]
    form = _build_alipay_form(
        out_trade_no=order_id, total_amount="199.00", trade_status="TRADE_SUCCESS", n=n_a, d=d_a,
    )
    resp1 = client.post("/api/payments/notify/alipay", headers=_HEADERS, data=form)
    assert resp1.text.strip() == "success"
    resp2 = client.post("/api/payments/notify/alipay", headers=_HEADERS, data=form)
    assert resp2.text.strip() == "success"

    verify = _verify_conn()
    grants = verify.execute(
        "SELECT COUNT(*) c FROM quota_ledger WHERE resource='video_addon_seconds' AND attempt_key=? AND reason='grant'",
        (order_id,),
    ).fetchone()
    assert grants["c"] == 1
    verify.close()
