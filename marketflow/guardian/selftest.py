"""Guardian end-to-end selftest (offline, no SDK / no network).

Proves the hardening invariants without touching a live wallet:
  * credential store: delegated agent credentials roundtrip encrypted; plaintext
    never on disk; a wallet or root key is refused outright.
  * one signer: only a user-root delegation with a fresh authority proof can get a
    signing client; every other record is refused, never routed elsewhere.
  * caps: a mandate's own cap is honored; a fat-finger value clamps to the
    ceiling (typo guard, not policy).
  * live gated: with GUARDIAN_LIVE_ENABLED absent, every SELL plans dry_run even
    with a fully valid guardian arm file. With it present + valid arm, it clears.
  * arm binding: an operator-written arm file can't arm a mandate; a guardian arm
    file for mandate A can't arm mandate B; arm requires a validated authority
    proof for every side the mode can sign.
  * exit-only: a BUY-side decision is never turned into an order by the exit path.
  * isolation: one mandate's fault doesn't stop another.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
def _write_guardian_arm(path, *, tenant_id, armed=True, mode="exit_only",
                        writer="guardian_service", ack="GUARDIAN_TENANT_APPROVES_AUTO_EXIT",
                        total=None):
    from marketflow.execution import orders as pmx
    obj = {
        "schema_version": pmx.ARM_STATE_SCHEMA_VERSION,
        "armed": armed, "mode": mode, "live_ack": ack, "written_by": writer,
        "tenant_id": tenant_id, "budget_epoch": "g-epoch",
        "budget_epoch_started_at": "2026-07-01T00:00:00Z",
    }
    if total is not None:
        obj["max_total_deploy_usd"] = total
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def selftest() -> dict:
    from cryptography.fernet import Fernet
    checks: dict[str, bool] = {}
    # Checks that could not run. Reported separately so PASS never absorbs them:
    # a skipped drift-detector is not a passing one.
    skipped: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="guardian_st_") as tmp:
        os.environ["MARKETFLOW_GUARDIAN_ROOT"] = tmp
        master_key = Fernet.generate_key()

        # reload store/executor so MARKETFLOW_GUARDIAN_ROOT takes effect. traps caches
        # paths off the store module at import, so a stale copy would write the
        # real runtime tree instead of the temp one.
        for m in (
            "store", "executor", "wallet", "traps", "http_api", "service",
            "onboarding", "authority", "risk_budget", "turnkey",
        ):
            sys.modules.pop(m, None)
        from marketflow.guardian import wallet as gwallet
        from marketflow.guardian import store as gstore
        from marketflow.guardian import executor as gexec
        from marketflow.guardian import turnkey as gturnkey
        from marketflow.execution import orders as pmx

        # --- credential store: only enclave agent credentials, never a private key ---
        tdir = gstore.tenant_dir("TEST1")
        _agent_secret = "agent-" + "d" * 58
        _delegated = {
            "key_backend": "turnkey_user_root",
            "turnkey_organization_id": "org-selftest", "turnkey_signer_address": "0x" + "5" * 40,
            "funder_address": "0x" + "6" * 40, "api_key": "k", "api_secret": "s", "passphrase": "p",
            "turnkey_entry_agent_private_key": _agent_secret, "turnkey_entry_agent_public_key": "02" + "a" * 64,
            "turnkey_exit_agent_private_key": _agent_secret, "turnkey_exit_agent_public_key": "02" + "b" * 64,
        }
        gwallet.encrypt_delegated_secrets(tdir, _delegated, master_key=master_key)
        back = gwallet.decrypt_secrets(tdir, master_key=master_key)
        checks["delegated_credentials_roundtrip"] = back == _delegated
        with open(os.path.join(tdir, gwallet.SECRETS_BLOB), "rb") as _bf:
            blob = _bf.read()
        checks["plaintext_credential_not_in_blob"] = _agent_secret.encode() not in blob
        wrong = Fernet.generate_key()
        try:
            gwallet.decrypt_secrets(tdir, master_key=wrong)
            checks["wrong_key_cannot_decrypt"] = False
        except Exception:
            checks["wrong_key_cannot_decrypt"] = True
        # The store refuses a wallet private key outright: holding one would be custody.
        for _field in ("private_key", "root_private_key", "mnemonic", "seed"):
            try:
                gwallet.encrypt_delegated_secrets(tdir, dict(_delegated, **{_field: "0x" + "1" * 64}),
                                                  master_key=master_key)
                checks[f"store_refuses_{_field}"] = False
            except gwallet.WalletError:
                checks[f"store_refuses_{_field}"] = True
        # And a tenant that is not enclave-backed cannot be given a signing client:
        # there is no local-key backend to fall back to.
        try:
            gexec.build_client_for_tenant({"private_key": "0x" + "1" * 64}, secret_dir=tdir)
            checks["non_enclave_tenant_cannot_sign"] = False
        except gturnkey.TurnkeyError:
            checks["non_enclave_tenant_cannot_sign"] = True
        except Exception:
            checks["non_enclave_tenant_cannot_sign"] = False
        gwallet.encrypt_delegated_secrets(tdir, _delegated, master_key=master_key)

        # Master-key creation is an explicit install action, never a side effect
        # of a web request or execution tick.
        explicit_key_path = os.path.join(tmp, "secrets", "explicit-master.txt")
        try:
            gwallet.load_master_key(path=explicit_key_path)
            checks["missing_master_key_fails_closed"] = False
        except gwallet.WalletError:
            checks["missing_master_key_fails_closed"] = True
        checks["explicit_master_key_initialization"] = bool(
            gwallet.initialize_master_key(path=explicit_key_path)
            and os.path.exists(explicit_key_path)
        )

        # --- a SELL decision on a held position ---
        position = {"token_id": "tok1", "held_shares": 100.0, "entry_price": 0.5,
                    "current_sell_price": 0.40, "break_even_probability": 0.40,
                    "market_slug": "st-market"}
        sell = {"decision": "STOP_LOSS_SELL", "token_id": "tok1", "market_slug": "st-market",
                "rule_fired": "stop_loss", "reason": "down 20%"}

        arm_path = gexec.tenant_arm_file("TEST1")

        # live NOT enabled globally -> dry_run even with a valid guardian arm
        _write_guardian_arm(arm_path, tenant_id="TEST1", total=gexec.TENANT_DEFAULT_TOTAL_USD)
        plan = gexec.plan_exit_for_decision("TEST1", sell, position)
        checks["sell_planned"] = plan is not None
        checks["dry_run_without_live_enabled"] = plan["will_execute_live"] is False
        # default caps = conservative starter values (user hasn't set any yet)
        checks["caps_default_conservative"] = (
            plan["fuses"]["checks"]["caps"]["max_total_deploy_usd_code"] == gexec.TENANT_DEFAULT_TOTAL_USD
        )

        # Caps stated by the mandate are honoured: they are its own risk policy
        user_entry = {"tenant_id": "TEST1", "rules": {"max_total_usd": 500.0, "max_per_trade_usd": 50.0}}
        user_caps = gexec.tenant_caps(user_entry)
        checks["user_cap_honored_above_default"] = (
            user_caps.max_total_deploy_usd == 500.0 and user_caps.max_per_trade_usd == 50.0
        )
        # a fat-finger cap clamps to the tenant ceiling (typo guard, not policy)
        fat_caps = gexec.tenant_caps({"tenant_id": "TEST1", "rules": {"max_total_usd": 9e9}})
        checks["fat_finger_cap_clamps_to_ceiling"] = (
            fat_caps.max_total_deploy_usd == gexec.TENANT_CAP_CEILING_TOTAL_USD
        )
        # an arm-file cap above the tenant's OWN cap still tampers (caps only tighten)
        _write_guardian_arm(arm_path, tenant_id="TEST1",
                            total=gexec.TENANT_CAP_CEILING_TOTAL_USD * 10.0)
        plan_fat = gexec.plan_exit_for_decision("TEST1", sell, position)
        checks["fat_cap_arm_clamps_down"] = plan_fat["fuses"]["checks"]["arm_state"]["cap_tamper"] is True

        # now enable live globally + valid arm -> live-cleared plan (exit_only allows SELL)
        _write_guardian_arm(arm_path, tenant_id="TEST1", total=gexec.TENANT_DEFAULT_TOTAL_USD)
        open(gstore.LIVE_ENABLED_FILE, "w").close()
        plan_live = gexec.plan_exit_for_decision("TEST1", sell, position)
        checks["live_cleared_when_enabled_and_armed"] = plan_live["will_execute_live"] is True

        # an operator-written arm (the single-operator path) must NOT arm a guardian tenant
        _write_guardian_arm(arm_path, tenant_id="TEST1", writer=pmx.ARM_WRITER, ack=pmx.LIVE_ACK_PHRASE, total=gexec.TENANT_DEFAULT_TOTAL_USD)
        plan_operator = gexec.plan_exit_for_decision("TEST1", sell, position)
        checks["operator_arm_cannot_arm_guardian"] = plan_operator["will_execute_live"] is False

        # a guardian arm for tenant A copied to tenant B cannot arm B (tenant binding)
        _write_guardian_arm(gexec.tenant_arm_file("TEST2"), tenant_id="TEST1", total=gexec.TENANT_DEFAULT_TOTAL_USD)  # wrong tenant_id inside
        gwallet.encrypt_delegated_secrets(gstore.tenant_dir("TEST2"), _delegated, master_key=master_key)
        plan_b = gexec.plan_exit_for_decision("TEST2", sell, position)
        checks["tenant_bound_arm_rejects_foreign"] = plan_b["will_execute_live"] is False

        os.remove(gstore.LIVE_ENABLED_FILE)

        # --- arm.py write path: gate + authority + roundtrip + validator ---
        sys.modules.pop("arm", None)
        from datetime import datetime, timezone
        from marketflow.guardian import arm as garm
        from marketflow.guardian import authority as gauth
        # A record that is not a user-root delegation is refused even with every
        # gate open and a valid proof on disk: there is no other kind of mandate
        # to arm, and a proof file cannot promote a record that does not say so.
        gstore.upsert_tenant({"tenant_id": "PLAIN1", "status": "ready"})
        _now_arm = datetime.now(timezone.utc).replace(microsecond=0)
        gauth.save_verified_authority(gauth._fixture(_now_arm, tenant_id="PLAIN1"),
                                      tenant_id="PLAIN1")
        # Arm needs a user-root registration with a validated authority proof.
        _arm_proof = gauth._fixture(_now_arm, tenant_id="TEST1")
        gauth.save_verified_authority(_arm_proof, tenant_id="TEST1")
        gstore.upsert_tenant({
            "tenant_id": "TEST1", "status": "ready",
            "authority_mode": gauth.MODE_TURNKEY_USER_ROOT,
            "signer_address": _arm_proof["wallet"]["signer_address"],
            "funder_address": _arm_proof["wallet"]["funder_address"],
        })
        user_root_gate = os.path.join(gstore.GUARDIAN_ROOT, gauth.USER_ROOT_ENABLED_FILENAME)
        open(user_root_gate, "w").close()
        # gate closed -> arm refused
        try:
            garm.arm_tenant("TEST1")
            checks["arm_refused_while_gate_closed"] = False
        except garm.ArmError:
            checks["arm_refused_while_gate_closed"] = True
        # gate open -> arm writes a file the executor's own validator accepts
        open(gstore.LIVE_ENABLED_FILE, "w").close()
        try:
            garm.arm_tenant("PLAIN1")
            checks["arm_refused_for_non_user_root_record"] = False
        except garm.ArmError as exc:
            checks["arm_refused_for_non_user_root_record"] = "not a user-root delegation" in str(exc)
        res = garm.arm_tenant("TEST1")
        checks["arm_writes_exit_only"] = res.get("mode") == "exit_only"
        st = garm.arm_status("TEST1")
        checks["arm_status_valid_via_validator"] = st["armed"] is True and st["mode"] == "exit_only"
        plan_armed = gexec.plan_exit_for_decision("TEST1", sell, position)
        checks["armed_tenant_sell_live_clears"] = plan_armed["will_execute_live"] is True
        garm.disarm_tenant("TEST1")
        checks["disarm_immediate"] = garm.arm_status("TEST1")["armed"] is False
        os.remove(gstore.LIVE_ENABLED_FILE)

        # exit-only: a BUY-side decision is never turned into an order by this path
        buy = {"decision": "ENTRY_SIGNAL", "token_id": "tok1", "rule_fired": "graduated_model"}
        checks["buy_decision_not_executed_by_exit_path"] = gexec.plan_exit_for_decision("TEST1", buy, position) is None

        # --- automated entry -------------------------------------------------
        # Every one of these asserts a REFUSAL. Entry has many ways to be denied
        # and exactly one way to proceed; the denials are what must be tested.
        # A deployment registers its own signal sources, so the test registers one
        # rather than relying on a name shipped in the code.
        _saved_entry_sources = gexec.ENTRY_SOURCE_ALLOWLIST
        gexec.ENTRY_SOURCE_ALLOWLIST = ("test_signal_source",)
        sig = {"source": "test_signal_source", "token_id": "tokE", "market_id": "0xmE",
               "market_slug": "flb-test", "side": "NO", "ask_price": 0.82,
               "max_price": 0.84, "model_probability": 0.9176,
               "resolution_confirmed_clean": True, "order_min_size": 5.0,
               "created_at": "2026-07-25T00:00:00Z"}
        tenant = {"tenant_id": "TEST1", "status": "ready",
                  "rules": {"max_total_usd": 200.0, "max_per_trade_usd": 20.0,
                            "risk_profile": "balanced"}}
        # a fixed book so these tests never touch the network
        book = {"bids": [{"price": "0.80", "size": "500"}],
                "asks": [{"price": "0.82", "size": "500"}], "tick_size": "0.01"}

        checks["entry_refused_while_entry_gate_closed"] = gexec.plan_entry_for_intent(
            "TEST1", sig, entry=tenant, book=book, available_usd=100.0) is None

        open(gstore.ENTRY_ENABLED_FILE, "w").close()
        try:
            checks["entry_refuses_unknown_source"] = gexec.plan_entry_for_intent(
                "TEST1", {**sig, "source": "some_new_model"}, entry=tenant,
                book=book, available_usd=100.0) is None
            checks["entry_refuses_when_daily_count_reached"] = gexec.plan_entry_for_intent(
                "TEST1", sig, entry=tenant, book=book, available_usd=100.0,
                opened_today=5) is None
            # live ask above what the signal authorised -> refuse, never chase
            checks["entry_refuses_when_price_ran_past_signal_max"] = gexec.plan_entry_for_intent(
                "TEST1", sig, entry=tenant, available_usd=100.0,
                book={"bids": [{"price": "0.84", "size": "500"}],
                      "asks": [{"price": "0.86", "size": "500"}]}) is None
            # a price outside the profile's evidence-backed band -> refuse
            checks["entry_refuses_outside_profile_band"] = gexec.plan_entry_for_intent(
                "TEST1", {**sig, "max_price": 0.99}, entry=tenant, available_usd=100.0,
                book={"bids": [{"price": "0.86", "size": "500"}],
                      "asks": [{"price": "0.88", "size": "500"}]}) is None
            # balance too small for the exchange minimum -> refuse
            checks["entry_refuses_when_balance_below_minimum"] = gexec.plan_entry_for_intent(
                "TEST1", sig, entry=tenant, book=book, available_usd=2.0) is None

            plan_e = gexec.plan_entry_for_intent("TEST1", sig, entry=tenant,
                                                 book=book, available_usd=100.0)
            checks["entry_plans_when_all_conditions_met"] = plan_e is not None
            g = (plan_e or {}).get("guardian") or {}
            # disarmed tenant -> planned but never live-cleared
            checks["entry_disarmed_stays_dry_run"] = (plan_e or {}).get("will_execute_live") is False
            # maker price sits inside the spread so a post-only order cannot cross
            checks["entry_maker_price_does_not_cross"] = (
                float((plan_e or {}).get("intent", {}).get("price") or 1.0) < 0.82)
            # balance is a ceiling: 98% headroom, never the full balance
            checks["entry_sizing_respects_balance_headroom"] = (
                float(g.get("estimated_notional_usd") or 0) <= 100.0 * 0.98 + 1e-6)
            # profile per_trade_pct (10% of 200 = 20) and per-trade cap both bind
            checks["entry_sizing_respects_profile_and_caps"] = (
                float(g.get("spend_ceiling_usd") or 0) <= 20.0 + 1e-6)
            checks["entry_prices_off_live_book"] = g.get("ask_price_source") == "live_book"

            # execute refuses if the gate closed between planning and signing
            os.remove(gstore.ENTRY_ENABLED_FILE)
            forced = dict(plan_e or {}, will_execute_live=True)
            rec = gexec.execute_entry_plan("TEST1", forced)
            checks["entry_execute_refuses_after_gate_closed"] = (
                rec.get("executed") is False and "gate closed" in str(rec.get("reason")))
        finally:
            if os.path.exists(gstore.ENTRY_ENABLED_FILE):
                os.remove(gstore.ENTRY_ENABLED_FILE)

        # arm: entry mode refused while its own ops gate is closed
        open(gstore.LIVE_ENABLED_FILE, "w").close()
        try:
            try:
                garm.arm_tenant("TEST1", mode="entry_allowlisted")
                checks["arm_entry_refused_while_entry_gate_closed"] = False
            except garm.ArmError:
                checks["arm_entry_refused_while_entry_gate_closed"] = True
            try:
                garm.arm_tenant("TEST1", mode="full")
                checks["arm_refuses_full_mode"] = False
            except garm.ArmError:
                checks["arm_refuses_full_mode"] = True
            open(gstore.ENTRY_ENABLED_FILE, "w").close()
            res_e = garm.arm_tenant("TEST1", mode="entry_allowlisted")
            checks["arm_writes_entry_allowlisted"] = res_e.get("mode") == "entry_allowlisted"
            checks["arm_entry_valid_via_validator"] = (
                garm.arm_status("TEST1")["mode"] == "entry_allowlisted")
            garm.disarm_tenant("TEST1")
            # Revoke the proof and the same arm is refused: authority is checked at
            # arm time, not remembered from an earlier success.
            gauth.mark_revocation_observed(
                "TEST1", turnkey_revoked_at=gstore.iso_now(),
                cancel_all_confirmed_at=gstore.iso_now())
            try:
                garm.arm_tenant("TEST1")
                checks["arm_refused_after_authority_revoked"] = False
            except garm.ArmError:
                checks["arm_refused_after_authority_revoked"] = True
        finally:
            for f in (gstore.LIVE_ENABLED_FILE, gstore.ENTRY_ENABLED_FILE, user_root_gate):
                if os.path.exists(f):
                    os.remove(f)

        # --- fleet fan-out guards -------------------------------------------
        sys.modules.pop("fanout", None)
        from marketflow.guardian import fanout as gfan
        ids = ["t1", "t2", "t3", "t4"]
        o1, o2 = gfan.tenant_order(ids, tick_seed="a"), gfan.tenant_order(ids, tick_seed="b")
        checks["fanout_order_is_deterministic"] = gfan.tenant_order(ids, tick_seed="a") == o1
        checks["fanout_order_rotates_between_ticks"] = o1 != o2
        checks["fanout_order_keeps_everyone"] = sorted(o1) == sorted(ids)
        b = gfan.TickBudget(share=0.25)
        checks["fanout_headroom_is_share_of_depth"] = b.headroom("m", 100.0) == 25.0
        b.commit("m", 25.0)
        checks["fanout_headroom_exhausts"] = b.headroom("m", 100.0) == 0.0
        checks["fanout_unknown_depth_imposes_no_limit"] = b.headroom("m2", None) is None
        st: dict = {}
        gfan.record_fleet_loss(gfan.FLEET_DAILY_DRAWDOWN_USD * 1.2, state=st)
        checks["fanout_fleet_halt_trips_on_daily_loss"] = gfan.entry_halted(st)["halted"] is True
        checks["fanout_fleet_halt_clear_when_small"] = gfan.entry_halted({})["halted"] is False

        # service isolation: one tenant faulting doesn't stop another
        sys.modules.pop("service", None)
        from marketflow.guardian import service as gsvc
        good = {"tenant_id": "TEST1", "status": "ready",
                "funder_address": None}  # no funder -> clean error, isolated
        r = gsvc.run_tenant(good)
        checks["missing_funder_isolated_error"] = r["error"] is not None and "funder" in str(r["error"]).lower()

        # Balance is trusted only from an on-chain balanceOf. None must mean "not
        # known" and nothing else: callers fall back to cap-only sizing with the
        # venue as backstop. Reading a failure as 0 would stop sizing silently;
        # trusting an indexer snapshot would keep spending money already gone.
        # Neither direction is acceptable.
        from marketflow import chain
        seen = []

        def _ok(rpc, body):
            seen.append(rpc)
            payload = json.loads(body)
            call = payload["params"][0]
            return {"result": hex(379311)} if (
                payload["method"] == "eth_call"
                and call["to"] == chain.PUSD
                and call["data"].startswith(chain.ERC20_BALANCE_OF)
                and call["data"].endswith("eeee000000000000000000000000000000000001")
            ) else {"error": "bad call shape"}

        checks["pusd_onchain_decodes"] = chain.erc20_balance(
            "0xEeEe000000000000000000000000000000000001", opener=_ok) == 0.379311
        checks["pusd_onchain_stops_at_first_rpc"] = len(seen) == 1
        tried = []

        def _dead_then_ok(rpc, body):
            tried.append(rpc)
            if len(tried) == 1:
                raise OSError("rpc down")
            return {"result": hex(1_500_000)}

        checks["pusd_onchain_falls_back"] = (
            chain.erc20_balance("0x" + "a" * 40, opener=_dead_then_ok) == 1.5
            and len(tried) == 2)
        checks["pusd_onchain_all_down_is_none"] = chain.erc20_balance(
            "0x" + "a" * 40, rpcs=("x", "y"),
            opener=lambda r, b: (_ for _ in ()).throw(OSError("down"))) is None
        checks["pusd_onchain_bad_result_is_none"] = chain.erc20_balance(
            "0x" + "a" * 40, rpcs=("x",), opener=lambda r, b: {"error": {"code": -32000}}) is None
        checks["pusd_onchain_rejects_malformed_addr"] = (
            chain.erc20_balance("not-an-address") is None
            and chain.erc20_balance("") is None)
        checks["collateral_no_funder_is_none"] = gexec.tenant_collateral_usd(None) is None
        checks["collateral_uses_injected_reader"] = gexec.tenant_collateral_usd(
            "0x" + "b" * 40, fetcher=lambda a: 12.5) == 12.5

        # --- Turnkey signing layer (offline: no enclave, no network) ---
        sys.modules.pop("turnkey", None)
        from marketflow.guardian import turnkey as gturnkey
        checks.update({f"turnkey_{k}": v for k, v in gturnkey.selftest().items()})
        from marketflow.guardian import authority as gauth
        from marketflow.guardian import risk_budget as grisk
        checks.update({f"authority_{k}": v for k, v in gauth.selftest().items()})
        checks.update({f"risk_budget_{k}": v for k, v in grisk.selftest().items()})

        # A user-root mandate carries only scoped P-256 agent credentials, and an
        # existing record of any other kind cannot be converted in place.
        from marketflow.guardian import onboarding as gonboard
        from datetime import datetime, timezone

        auth_record = gauth._fixture(datetime.now(timezone.utc).replace(microsecond=0))

        # A root one device can exercise alone is refused outright: one compromised
        # device would otherwise be total loss for that mandate.
        _single_sig = json.loads(json.dumps(auth_record))
        _single_sig["root"]["quorum_threshold"] = 1
        try:
            gauth.validate_user_root_authority(
                _single_sig, expected_tenant_id=_single_sig["tenant_id"], side="BUY",
                expected_signer_address=_single_sig["wallet"]["signer_address"],
                expected_funder_address=_single_sig["wallet"]["funder_address"],
                maker_amount_base_units=None, require_gate=False)
            checks["authority_refuses_single_signature_root"] = False
        except gauth.AuthorityError as exc:
            checks["authority_refuses_single_signature_root"] = "quorum" in str(exc)

        delegated = {
            "key_backend": gturnkey.KEY_BACKEND_TURNKEY_USER_ROOT,
            "turnkey_organization_id": "suborg-tenant-1",
            "turnkey_signer_address": "0x" + "1" * 40,
            "funder_address": "0x" + "2" * 40,
            "api_key": "clob-key", "api_secret": "clob-secret", "passphrase": "clob-pass",
            "turnkey_entry_agent_private_key": "ab" * 32,
            "turnkey_entry_agent_public_key": "02" + "4" * 64,
            "turnkey_exit_agent_private_key": "cd" * 32,
            "turnkey_exit_agent_public_key": "02" + "4" * 64,
        }
        user_root_entry = gonboard.register_user_root_delegation(
            "AUTH1", authority_record=auth_record,
            delegated_secrets=delegated, master_key=master_key,
        )
        checks["user_root_registration_is_ready_and_noncustodial"] = (
            user_root_entry["status"] == "ready"
            and user_root_entry["custody_mode"] == "noncustodial_user_root"
            and user_root_entry["authority_mode"] == gauth.MODE_TURNKEY_USER_ROOT
        )
        stored_delegated = gwallet.decrypt_secrets(
            gstore.tenant_dir("AUTH1"), master_key=master_key,
        )
        checks["user_root_blob_has_no_eoa_or_root_key"] = (
            "private_key" not in stored_delegated
            and "api_private_key" not in stored_delegated
            and "turnkey_agent_private_key" not in stored_delegated
        )
        try:
            gwallet.encrypt_delegated_secrets(
                gstore.tenant_dir("BADROOT"),
                {**delegated, "private_key": "11" * 32},
                master_key=master_key,
            )
            checks["delegated_blob_rejects_eoa_key"] = False
        except gwallet.WalletError:
            checks["delegated_blob_rejects_eoa_key"] = True

        # The service reserves cumulative BUY budget before the executor sees a
        # live plan. It is isolated per tenant.
        original_plan_entry = gexec.plan_entry_for_intent
        original_collateral = gexec.tenant_collateral_usd
        original_execute_entry = gexec.execute_entry_plan
        captured_reservation: dict[str, str] = {}
        open(gstore.ENTRY_ENABLED_FILE, "w").close()
        try:
            gexec.tenant_collateral_usd = lambda _address: 100.0
            gexec.plan_entry_for_intent = lambda *_a, **_k: {
                "will_execute_live": True,
                "mode": "LIVE_PLAN",
                "idempotency_key": "service-risk-1",
                "guardian": {
                    "market_id": "market-service",
                    "market_slug": "market-service",
                    "estimated_notional_usd": 5.0,
                    "risk_profile": "balanced",
                },
            }

            def _fake_execute_with_reservation(tid, plan):
                rid = str(plan["guardian"].get("risk_reservation_id") or "")
                captured_reservation["id"] = rid
                grisk.validate_reservation(
                    tid, rid, market_id="market-service", notional_usd=5.0,
                )
                grisk.release_entry(tid, rid, reason="selftest_no_submission")
                return {"executed": False}

            gexec.execute_entry_plan = _fake_execute_with_reservation
            service_rows = gsvc.run_entries(
                {
                    "tenant_id": "SERVICEAUTH", "status": "ready", "authority_mode": "turnkey_user_root",
                    "funder_address": "0x" + "2" * 40,
                },
                signals=[{"market_id": "market-service", "market_slug": "market-service"}],
                budget=gfan.TickBudget(), fleet_halt={"halted": False},
            )
            checks["service_reserves_user_root_risk_before_execute"] = bool(
                captured_reservation.get("id") and service_rows
            )
        finally:
            gexec.plan_entry_for_intent = original_plan_entry
            gexec.tenant_collateral_usd = original_collateral
            gexec.execute_entry_plan = original_execute_entry
            if os.path.exists(gstore.ENTRY_ENABLED_FILE):
                os.remove(gstore.ENTRY_ENABLED_FILE)

        # The executor rechecks the same reservation immediately before signing
        # and commits accepted resting orders against the cumulative budget.
        class _FakeClient:
            def close(self):
                return None

        reservation = grisk.reserve_entry(
            "AUTH1", idempotency_key="executor-risk-1", market_id="market-exec",  # gitleaks:allow
            notional_usd=5.0, limits=grisk.RiskLimits(),
        )
        entry_plan = {
            "will_execute_live": True,
            "guardian": {
                "market_id": "market-exec", "market_slug": "market-exec",
                "estimated_notional_usd": 5.0, "builder_fee_bps": 0,
                "risk_reservation_id": reservation["reservation_id"],
            },
        }
        original_timeout = gexec._with_hard_timeout
        original_execute_order = pmx.execute_order
        original_leak_check = pmx.assert_no_secret_leak
        open(gstore.LIVE_ENABLED_FILE, "w").close()
        open(gstore.ENTRY_ENABLED_FILE, "w").close()
        try:
            gexec._with_hard_timeout = lambda _sec, _label, _fn: _FakeClient()
            pmx.execute_order = lambda *_a, **_k: {"executed": True}
            pmx.assert_no_secret_leak = lambda *_a, **_k: []
            executed = gexec.execute_entry_plan(
                "AUTH1", entry_plan, intent=object(), master_key=master_key,
            )
            states = grisk._usage(grisk.load_events("AUTH1"), now=10**10)["reservations"]
            checks["executor_commits_user_root_risk_after_accept"] = (
                executed["executed"] is True
                and states[reservation["reservation_id"]]["status"] == grisk.EVENT_COMMITTED
            )
        finally:
            gexec._with_hard_timeout = original_timeout
            pmx.execute_order = original_execute_order
            pmx.assert_no_secret_leak = original_leak_check
            for path in (gstore.LIVE_ENABLED_FILE, gstore.ENTRY_ENABLED_FILE):
                if os.path.exists(path):
                    os.remove(path)

        # No active reservation for exactly this order, no signature: the executor
        # refuses a live BUY that did not pass through the risk budget, and it does
        # so before any client is built.
        other = grisk.reserve_entry(
            "AUTH1", idempotency_key="executor-risk-2", market_id="market-reserved",  # gitleaks:allow
            notional_usd=5.0, limits=grisk.RiskLimits(),
        )
        built: list[int] = []
        open(gstore.LIVE_ENABLED_FILE, "w").close()
        open(gstore.ENTRY_ENABLED_FILE, "w").close()
        try:
            gexec._with_hard_timeout = lambda _sec, _label, _fn: built.append(1) or _FakeClient()
            for label, extra in (
                ("missing", {}),
                ("foreign_market", {"risk_reservation_id": other["reservation_id"]}),
            ):
                unreserved = {"will_execute_live": True, "guardian": {
                    "market_id": "market-unreserved", "estimated_notional_usd": 5.0, **extra}}
                try:
                    gexec.execute_entry_plan("AUTH1", unreserved, intent=object(),
                                             master_key=master_key)
                    refused = False
                except grisk.RiskBudgetError:
                    refused = True
                except Exception:  # noqa: BLE001 - any other failure means the budget did not refuse first
                    refused = False
                checks[f"executor_refuses_buy_with_{label}_reservation"] = refused and not built
        finally:
            gexec._with_hard_timeout = original_timeout
            grisk.release_entry("AUTH1", other["reservation_id"], reason="selftest_cleanup")
            for path in (gstore.LIVE_ENABLED_FILE, gstore.ENTRY_ENABLED_FILE):
                if os.path.exists(path):
                    os.remove(path)

        # Even with Turnkey's fleet route open, a public-key mismatch is refused
        # before any SDK/network client can be constructed.
        gate = os.path.join(gstore.GUARDIAN_ROOT, gturnkey.TURNKEY_ENABLED_FILENAME)
        open(gate, "w").close()
        try:
            bad_agent = dict(delegated)
            bad_agent["turnkey_entry_agent_public_key"] = "03" + "5" * 64
            try:
                gexec.build_client_for_tenant(
                    bad_agent, secret_dir=gstore.tenant_dir("AUTH1"),
                    tenant_id="AUTH1", side="BUY", maker_amount_base_units=1_000_000,
                )
                checks["executor_refuses_authority_agent_mismatch"] = False
            except gauth.AuthorityError:
                checks["executor_refuses_authority_agent_mismatch"] = True
        finally:
            os.remove(gate)

        # Only a user-root delegation can get a signing client. A record naming any
        # other backend is refused with the gate closed AND with it open: there is
        # no second signer for a closed gate or a stale record to fall back to.
        for label, record in (
            ("platform_backend", {"key_backend": "turnkey", "private_key": "0x" + "1" * 64}),
            ("local_key_record", {"private_key": "0x" + "1" * 64}),
        ):
            for gate_open in (False, True):
                gate = os.path.join(gstore.GUARDIAN_ROOT, gturnkey.TURNKEY_ENABLED_FILENAME)
                if gate_open:
                    open(gate, "w").close()
                try:
                    gexec.build_client_for_tenant(record, secret_dir=tdir, tenant_id="AUTH1", side="SELL")
                    refused = False
                except gturnkey.TurnkeyError:
                    refused = True
                except Exception:
                    refused = False
                finally:
                    if os.path.exists(gate):
                        os.remove(gate)
                state = "open" if gate_open else "closed"
                checks[f"signing_refuses_{label}_gate_{state}"] = refused

        # The SDK must keep emitting integer order amounts: a string makerAmount is
        # DENIED by the enclave cap even when under it, so a silent type change
        # upstream would stop every Turnkey-backed trade (fail-closed, but opaque).
        import inspect
        try:
            from polymarket.models.clob.orders import SignedOrder as _SDKSignedOrder
            _ann = getattr(_SDKSignedOrder, "__annotations__", {})
            checks["turnkey_sdk_amounts_are_int"] = (
                _ann.get("maker_amount") is int and _ann.get("taker_amount") is int)
            from polymarket._internal.actions.orders import typed_data as _sdk_typed
            _src = inspect.getsource(_sdk_typed)
            # Guardian's policy pins these two literals; if the SDK renames either,
            # every allowed condition silently matches nothing.
            checks["turnkey_sdk_domain_literal_unchanged"] = (
                f'"{gturnkey.CLOB_DOMAIN_NAME}"' in _src)
            checks["turnkey_sdk_nested_primary_type_unchanged"] = (
                f'"{gturnkey.CLOB_ORDER_PRIMARY_TYPE}"' in _src)
        except (ImportError, OSError, TypeError) as exc:
            # NOT fail-open. These three exist to catch SDK drift, so marking them
            # green when the SDK is absent would launder "not checked" into
            # "checked and fine" — exactly the signal they were added to provide.
            # `getsource` can also raise OSError/TypeError, which previously
            # crashed the whole run.
            skipped["turnkey_sdk_contract_pins"] = f"{type(exc).__name__}: {exc}"

        # --- event-exposure display route (read-only presentation) ---
        from marketflow.guardian import http_api as ghttp
        ev = ghttp._tenant_exposure_view("NOSUCHTENANT")
        checks["exposure_unknown_tenant_soft"] = (
            ev.get("available") is False and ev.get("reason") == "not_enrolled")
        # With no Deposit Wallet deployed the answer is "none", stated honestly.
        # Falling back to the signer EOA is forbidden: it is a different address,
        # and reading it would report somebody else's portfolio as this mandate's.
        gstore.upsert_tenant({"tenant_id": "EXPO1", "status": "paused", "funder_address": None})
        ev2 = ghttp._tenant_exposure_view("EXPO1")
        checks["exposure_no_deposit_wallet_is_honest"] = (
            ev2.get("available") is False
            and ev2.get("reason") == "deposit_wallet_not_deployed")
        src = open(os.path.join(os.path.dirname(os.path.abspath(ghttp.__file__)),
                                "http_api.py"), encoding="utf-8").read()
        # Scope the source-order assertion to the resident _Handler: the route
        # must be dispatched from do_GET, never reachable from do_POST.
        resident_src = src.split("class _Handler", 1)[1]
        checks["exposure_route_is_get_only"] = (
            '"/guardian/exposure"' in resident_src.split("def do_POST", 1)[0])
        checks["exposure_route_no_gamma_write"] = "use_gamma=False" in src

        # --- structural traps (C1 cheap ticket / C2 night lottery / C3 zombie) ---
        from marketflow.guardian import traps as gtraps

        checks["trap_cheap_trips_below_dime"] = gtraps.check_cheap_ticket(0.06)["tripped"]
        # 0.10 is the edge of the measured band, not inside it.
        checks["trap_cheap_clears_at_floor"] = not gtraps.check_cheap_ticket(0.10)["tripped"]
        checks["trap_cheap_ignores_unknown_price"] = not gtraps.check_cheap_ticket(None)["tripped"]

        checks["trap_night_window_edges"] = (
            gtraps.in_night_window(18.0) and gtraps.in_night_window(23.5)
            and gtraps.in_night_window(1.99) and not gtraps.in_night_window(2.0)
            and not gtraps.in_night_window(17.99))
        # Without a known zone the rule does not apply — never approximated by UTC.
        _saved_night0 = gtraps.NIGHT_RULE_ENABLED
        gtraps.NIGHT_RULE_ENABLED = True
        try:
            checks["trap_night_needs_known_local_hour"] = (
                not gtraps.check_night_lottery(0.15, None)["tripped"]
                and gtraps.check_night_lottery(0.15, None)["applicable"] is False)
        finally:
            gtraps.NIGHT_RULE_ENABLED = _saved_night0
        checks["trap_night_off_by_default"] = (
            gtraps.NIGHT_RULE_ENABLED is False
            and not gtraps.check_night_lottery(0.15, 22.0)["tripped"])
        _saved_night = gtraps.NIGHT_RULE_ENABLED
        gtraps.NIGHT_RULE_ENABLED = True
        try:
            checks["trap_night_trips_in_own_night"] = gtraps.check_night_lottery(0.15, 22.0)["tripped"]
            checks["trap_night_clears_by_day"] = not gtraps.check_night_lottery(0.15, 12.0)["tripped"]
        finally:
            gtraps.NIGHT_RULE_ENABLED = _saved_night
        # C1's floor is above C2's: a 0.15 daytime buy is C2's business only.
        checks["trap_night_floor_above_cheap_floor"] = (
            gtraps.NIGHT_PRICE_FLOOR > gtraps.CHEAP_PRICE_FLOOR)

        # timezone inference: a wallet silent 22:00-03:00 UTC has its quiet
        # midpoint at bin 00 UTC, which is local 03:00 -> UTC+3.
        hist = [8] * 24
        for h in (22, 23, 0, 1, 2):
            hist[h] = 0
        off, ratio = gtraps.infer_tz_offset(hist)
        checks["trap_tz_infers_offset_from_silence"] = off == 3 and ratio == 0.0
        checks["trap_tz_flat_activity_undetermined"] = gtraps.infer_tz_offset([5] * 24)[0] is None
        checks["trap_tz_empty_undetermined"] = gtraps.infer_tz_offset([0] * 24)[0] is None

        # Our own automation's fills carry the schedule of a robot, not a bedtime;
        # they must not reach the histogram the zone is read from.
        gstore.upsert_tenant({"tenant_id": "TZ1", "status": "ready",
                              "funder_address": "0xabc"})
        os.makedirs(gstore.tenant_dir("TZ1"), exist_ok=True)
        with open(gexec.tenant_ledger_file("TZ1"), "w", encoding="utf-8") as _lg:
            _lg.write(json.dumps({"generated_at": "2026-08-01T09:00:00Z"}) + "\n")
        own_ts = 1785574800.0  # 2026-08-01T09:00:00Z
        checks["trap_tz_reads_own_order_times"] = gtraps.own_order_times("TZ1") == [own_ts]
        mixed = [{"timestamp": own_ts + 60}, {"timestamp": own_ts + 3600}]
        h_mix, n_mix = gtraps.histogram_from_rows(mixed, exclude_times=[own_ts])
        checks["trap_tz_excludes_own_automation_fills"] = (n_mix == 1 and h_mix[10] == 1)
        good = gtraps.refresh_tz_profile("TZ1", "0xabc", now_ts=1000.0,
                                         hours_fetcher=lambda _a, **_k: (hist, 200, True))
        checks["trap_tz_profile_persisted"] = (good["offset_hours"] == 3
                                               and gtraps.load_tz_profile("TZ1")["offset_hours"] == 3)
        # A failed read must not overwrite a known zone with "unknown": the night
        # rule would silently stop applying every time the feed hiccups.
        stale = gtraps.refresh_tz_profile("TZ1", "0xabc", now_ts=1000.0 + 2 * gtraps.TZ_PROFILE_TTL_SEC,
                                          hours_fetcher=lambda _a, **_k: ([0] * 24, 0, False))
        checks["trap_tz_fetch_failure_keeps_known_zone"] = stale["offset_hours"] == 3
        thin = gtraps.refresh_tz_profile("TZ2", "0xdef", now_ts=1000.0,
                                         hours_fetcher=lambda _a, **_k: (hist, 5, True))
        checks["trap_tz_thin_history_no_zone"] = (thin["offset_hours"] is None
                                                  and thin["reason"] == "insufficient_history")
        declared = gtraps.refresh_tz_profile(
            "TZ3", None, entry={"rules": {"tz_offset_hours": -5}}, now_ts=1000.0,
            hours_fetcher=lambda _a, **_k: ([0] * 24, 0, True))
        checks["trap_tz_user_declared_wins"] = (declared["offset_hours"] == -5
                                                and declared["source"] == gtraps.TZ_SOURCE_DECLARED)

        # modes: shadow by default, enforce only when deliberately promoted
        checks["trap_default_mode_is_shadow"] = (
            gtraps.rule_mode(gtraps.RULE_CHEAP_TICKET) == gtraps.MODE_SHADOW)
        shadow = gtraps.screen_buy(price=0.05, tenant_id="TZ1", tz_offset_hours=2,
                                   now_ts=own_ts, log=False)
        checks["trap_shadow_records_without_blocking"] = (
            shadow["tripped"] == [gtraps.RULE_CHEAP_TICKET] and shadow["blocked"] is False)
        gtraps.set_rule_mode(gtraps.RULE_CHEAP_TICKET, gtraps.MODE_ENFORCE)
        enforced = gtraps.screen_buy(price=0.05, tenant_id="TZ1", tz_offset_hours=2,
                                     now_ts=own_ts)
        checks["trap_enforce_blocks"] = enforced["blocked"] is True
        # A corrupted mode file must fall back to shadow, never start blocking.
        with open(gtraps.mode_file(gtraps.RULE_CHEAP_TICKET), "w", encoding="utf-8") as _mf:
            _mf.write("ENFORCE_ALL_THE_THINGS\n")
        checks["trap_bad_mode_file_falls_back_to_shadow"] = (
            gtraps.rule_mode(gtraps.RULE_CHEAP_TICKET) == gtraps.MODE_SHADOW)
        gtraps.set_rule_mode(gtraps.RULE_CHEAP_TICKET, gtraps.MODE_OFF)
        off = gtraps.screen_buy(price=0.05, tenant_id="TZ1", tz_offset_hours=2,
                                now_ts=own_ts, log=False)
        checks["trap_off_mode_never_trips"] = gtraps.RULE_CHEAP_TICKET not in off["tripped"]
        gtraps.set_rule_mode(gtraps.RULE_CHEAP_TICKET, gtraps.MODE_SHADOW)
        with open(gtraps.TRAPS_LOG, encoding="utf-8") as _lf:
            trap_log = [json.loads(x) for x in _lf if x.strip()]
        checks["trap_log_records_every_trip"] = (
            len(trap_log) >= 1 and any(r.get("blocked") for r in trap_log))

        # zombie: what the book already wrote off, vs what is still claimable
        zpos = [
            {"held_shares": 100.0, "entry_price": 0.30, "current_sell_price": 0.004,
             "market_slug": "dead-1", "outcome": "Yes"},
            {"held_shares": 50.0, "entry_price": 0.40, "current_sell_price": 0.60,
             "market_slug": "alive-1", "outcome": "No"},
            {"held_shares": 10.0, "entry_price": 0.50, "current_sell_price": 1.0,
             "current_value_usd": 10.0, "redeemable": True, "market_slug": "won-1", "outcome": "Yes"},
        ]
        zrep = gtraps.zombie_report(zpos)
        checks["trap_zombie_counts_dead_only"] = (zrep["n_zombie"] == 1
                                                  and zrep["zombie_cost_usd"] == 30.0)
        checks["trap_zombie_separates_claimable"] = (zrep["n_redeemable"] == 1
                                                     and zrep["redeemable_value_usd"] == 10.0)
        # 30 of (30 + 20 + 5) open cost basis
        checks["trap_zombie_share_of_open_cost"] = zrep["zombie_share_of_open_cost"] == 0.5455
        first = gtraps.zombie_check("TZ1", zpos)
        second = gtraps.zombie_check("TZ1", zpos)
        checks["trap_zombie_prompts_once_per_ticket"] = (
            first["new_items"] == ["dead-1|Yes"] and second["new_items"] == [])

        # THE invariant: no trap mode can stand between a tenant and an exit.
        for _r in (gtraps.RULE_CHEAP_TICKET, gtraps.RULE_NIGHT_LOTTERY, gtraps.RULE_ZOMBIE):
            gtraps.set_rule_mode(_r, gtraps.MODE_ENFORCE)
        cheap_pos = {**position, "current_sell_price": 0.02, "break_even_probability": 0.02}
        exit_plan = gexec.plan_exit_for_decision("TEST1", sell, cheap_pos)
        checks["trap_never_blocks_an_exit"] = exit_plan is not None
        # ... and the entry side does refuse the same price. The fleet entry gate
        # has to be open or this would pass for an unrelated reason.
        entry_gate = gstore.ENTRY_ENABLED_FILE
        os.makedirs(os.path.dirname(entry_gate), exist_ok=True)
        open(entry_gate, "w").close()
        try:
            blocked_entry = gexec.plan_entry_for_intent(
                "TZ1", {"source": "test_signal_source", "token_id": "tokZ", "ask_price": 0.05},
                entry={"tenant_id": "TZ1"}, book={})
        finally:
            os.remove(entry_gate)
        with open(gtraps.TRAPS_LOG, encoding="utf-8") as _lf:
            auto_rows = [json.loads(x) for x in _lf if '"automated_entry"' in x]
        checks["trap_enforced_entry_refused"] = (
            blocked_entry is None
            and any(r.get("blocked") and gtraps.RULE_CHEAP_TICKET in (r.get("blocked_by") or [])
                    for r in auto_rows))
        for _r in (gtraps.RULE_CHEAP_TICKET, gtraps.RULE_NIGHT_LOTTERY, gtraps.RULE_ZOMBIE):
            gtraps.set_rule_mode(_r, gtraps.MODE_SHADOW)

        # Drift guard: the falsified blanket claim must not reappear in the code
        # that talks to users. "Night trading loses" is NULL (p=0.9528); only the
        # sub-$0.20 band inside that window is real.
        banned = ("trading at night is worse", "night trading loses",
                  "night trading is worse")
        guardian_src = ""
        for _f in sorted(os.listdir(HERE)):
            if _f.endswith(".py") and _f != "selftest.py":  # this file holds the list
                with open(os.path.join(HERE, _f), encoding="utf-8") as _sf:
                    guardian_src += _sf.read().lower()
        checks["trap_no_falsified_night_claim_in_source"] = not any(
            b.lower() in guardian_src for b in banned)

    report = {"PASS": all(checks.values()), "checks": checks}
    if skipped:
        report["skipped"] = skipped
    return report


if __name__ == "__main__":
    rep = selftest()
    print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0 if rep["PASS"] else 1)
