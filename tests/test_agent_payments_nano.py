"""The Nano rail (`infra/agent_payments/nano.py`, docs/context/architecture/agent-payments.md).

Only the node is replaced: a fake `block_info` built from the challenge the rail itself issued.
Standard library only, so these also run as `python -m unittest tests.test_agent_payments_nano`
with `src` on the path.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest import mock

from treg.infra.agent_payments import Offer, PaymentRejected, Unconfirmed
from treg.infra.agent_payments import nano
from treg.infra.agent_payments.nano import NanoRail

GENESIS_KEY = "E89208DD038FBB269987689621D52292AE9C35941A7484756ECCED92A65093BA"
GENESIS = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
PAY_TO = "nano_11131a3ia3a81w61k4id3i8iw5ri46b3871o4rdji8at5eg3t9izij86w3hz"  # key 00 01 .. 1f
PAYER = nano.address_of(bytes([7] * 32))
OTHER = nano.address_of(bytes([9] * 32))
SECRET = "s" * 40
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
BLOCK = "AB" * 32


def offer(path: str = "/call/tikhub.tiktok.video.comments", amount_micro: int = 50_000) -> Offer:
    return Offer(amount_micro=amount_micro, method="GET", path=path, query="",
                 resource_url=f"https://treg.example{path}", description="a call", host="treg.example")


def decode(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def encode(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


class FakeNode:
    def __init__(self) -> None:
        self.blocks: dict[str, dict] = {}
        self.reads = 0
        self.down = False

    async def block_info(self, block_hash: str) -> dict | None:
        self.reads += 1
        if self.down:
            raise nano.NodeUnavailable("down")
        return self.blocks.get(block_hash)


def send_block(*, raw: int, representative: str, pay_to: str = PAY_TO, account: str = PAYER,
               confirmed: str = "true", sent_at: datetime = NOW, subtype: str = "send") -> dict:
    return {"block_account": account, "amount": str(raw), "confirmed": confirmed,
            "local_timestamp": str(int(sent_at.timestamp())), "subtype": subtype,
            "contents": {"type": "state", "account": account, "representative": representative,
                         "link_as_account": pay_to, "balance": "1"}}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Accounts(unittest.TestCase):
    def test_known_addresses(self) -> None:
        self.assertEqual(nano.address_of(bytes.fromhex(GENESIS_KEY)), GENESIS)
        self.assertEqual(nano.public_key_of(GENESIS).hex().upper(), GENESIS_KEY)
        self.assertEqual(nano.address_of(bytes(32)),
                         "nano_1111111111111111111111111111111111111111111111111111hifc8npp")
        self.assertEqual(nano.normalize("xrb_" + GENESIS[5:]), GENESIS)

    def test_checksum_and_shape_are_checked(self) -> None:
        broken = GENESIS[:-1] + ("1" if GENESIS[-1] != "1" else "3")
        for bad in (broken, GENESIS[:-1], "0x" + "ab" * 20, "", "nano_" + "2" * 60):
            with self.assertRaises(ValueError):
                nano.public_key_of(bad)
        self.assertFalse(nano.same_account(broken, GENESIS))

    def test_raw_is_derived_and_rounded_up(self) -> None:
        self.assertEqual(nano.raw_for(50_000, Fraction(5, 4)), 625 * 10 ** 26)  # $0.05 at 1.25
        self.assertEqual(nano.raw_for(1_000_000, Fraction(1)), nano.RAW_PER_XNO)
        self.assertEqual(nano.raw_for(1, Fraction(1, 3)), 333_333_333_333_333_333_333_334)
        self.assertEqual(nano.parse_rate("1.25"), Fraction(5, 4))
        for bad in ("0", "-1", "abc", ""):
            with self.assertRaises(ValueError):
                nano.parse_rate(bad)

    def test_representative_matches_the_binding_reference_vector(self) -> None:
        # xno-binding v1 option C, computed with its reference `commitment_rep_account`.
        self.assertEqual(nano.representative_for("order-1", PAY_TO, 625 * 10 ** 26),
                         "nano_14an3sjsrybygqi9chqe31mp1x7zzf7kmts8g8mxjrqubsc7nsrxip4ppzz8")


class Rail(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.node = FakeNode()
        self.clock = Clock()
        self.rail = self.make()
        self.remote_checks = 0

    def make(self, **kw) -> NanoRail:
        args = {"pay_to": PAY_TO, "secret": SECRET, "xno_per_usd": "1.25", "node": self.node,
                "clock": self.clock}
        args.update(kw)
        return NanoRail(**args)

    async def before_remote(self) -> None:
        self.remote_checks += 1

    async def accepted(self, paid: Offer | None = None) -> dict:
        headers = await self.rail.challenge(paid or offer())
        self.assertEqual([name for name, _ in headers], [nano.CHALLENGE_HEADER])
        return decode(headers[0][1])["accepts"][0]

    async def pay(self, accepted: dict, block: str = BLOCK, **kw) -> str:
        args = {"raw": int(accepted["amount"]), "representative": accepted["extra"]["representative"]}
        args.update(kw)
        self.node.blocks[block] = send_block(**args)
        return encode({"order": accepted["extra"]["order"], "block": block})

    async def refused(self, credential: str, paid: Offer | None = None) -> str:
        with self.assertRaises(PaymentRejected) as caught:
            await self.rail.verify(credential, paid or offer(), before_remote=self.before_remote)
        return caught.exception.code

    def test_off_without_its_settings(self) -> None:
        with self.assertRaises(ValueError):
            self.make(pay_to=PAY_TO[:-1] + "1")
        with self.assertRaises(ValueError):
            self.make(secret="short")
        with self.assertRaises(ValueError):
            self.make(node=None)
        with self.assertRaises(ValueError):
            self.make(xno_per_usd="0")

    async def test_challenge_names_the_exact_offer(self) -> None:
        accepted = await self.accepted()
        self.assertEqual((accepted["scheme"], accepted["network"], accepted["payTo"]),
                         ("exact", "nano:mainnet", PAY_TO))
        self.assertEqual(accepted["amount"], str(625 * 10 ** 26))
        self.assertEqual(accepted["extra"]["representative"],
                         nano.representative_for(accepted["extra"]["order"], PAY_TO, 625 * 10 ** 26))
        self.assertEqual(await self.rail.challenge(offer(), pay=False, prove=True), [])
        self.assertFalse(self.rail.proves(offer()))
        again = await self.accepted()
        self.assertNotEqual(again["extra"]["order"], accepted["extra"]["order"])

    async def test_a_confirmed_bound_send_pays_once_per_block(self) -> None:
        credential = await self.pay(await self.accepted())
        self.assertEqual(self.rail.presented({nano.PAYMENT_HEADER: credential}), credential)
        verified = await self.rail.verify(credential, offer(), before_remote=self.before_remote)
        self.assertEqual(verified.proof_id, "nano:nano:mainnet:" + BLOCK.lower())
        self.assertEqual((verified.payer, verified.pay_to, verified.asset), (PAYER, PAY_TO, "xno"))
        self.assertEqual(verified.amount_micro, 50_000)
        self.assertFalse(verified.deferrable)
        self.assertEqual(self.remote_checks, 1)
        settlement = await self.rail.settle(verified, offer())
        self.assertEqual(settlement.reference, BLOCK)
        self.assertEqual(decode(settlement.receipt_headers[0][1])["transaction"], BLOCK)
        # The same block presented again, even to a restarted process, has the same proof id, which
        # `AgentPayment.proof_id`'s UNIQUE key refuses.
        restarted = self.make()
        self.assertEqual((await restarted.verify(credential, offer())).proof_id, verified.proof_id)

    async def test_a_block_for_another_request_is_refused(self) -> None:
        mine, theirs = await self.accepted(), await self.accepted()
        credential = await self.pay(mine, representative=theirs["extra"]["representative"])
        self.assertEqual(await self.refused(credential), "invalid_challenge")

    async def test_an_order_for_another_route_or_price_is_refused_before_the_node(self) -> None:
        credential = await self.pay(await self.accepted())
        self.assertEqual(await self.refused(credential, offer(path="/call/other.tool")), "invalid_challenge")
        self.assertEqual(await self.refused(credential, offer(amount_micro=60_000)), "invalid_challenge")
        self.assertEqual((self.node.reads, self.remote_checks), (0, 0))
        forged = encode({"order": "00" * 16 + ".9999999999.AAAAAAAAAAAAAAAAAAAAAA", "block": BLOCK})
        self.assertEqual(await self.refused(forged), "invalid_challenge")
        truncated = (await self.pay(await self.accepted()))[:-3] + "!!!"
        self.assertEqual(await self.refused(truncated), "malformed_credential")
        bad_hash = encode({"order": (await self.accepted())["extra"]["order"], "block": "zz"})
        self.assertEqual(await self.refused(bad_hash), "malformed_credential")
        self.assertEqual(self.node.reads, 0)

    async def test_another_secret_does_not_accept_the_order(self) -> None:
        credential = await self.pay(await self.accepted())
        self.rail = self.make(secret="t" * 40)
        self.assertEqual(await self.refused(credential), "invalid_challenge")

    async def test_wrong_amount_recipient_or_kind_is_refused(self) -> None:
        accepted = await self.accepted()
        raw = int(accepted["amount"])
        for kw, code in (({"raw": raw - 1}, "verification_failed"),
                         ({"raw": raw + 1}, "verification_failed"),
                         ({"pay_to": OTHER}, "verification_failed"),
                         ({"subtype": "receive"}, "verification_failed"),
                         ({"confirmed": "false"}, "payment_unconfirmed"),
                         ({"sent_at": NOW + timedelta(hours=1)}, "verification_failed"),
                         ({"account": "nano_nobody"}, "verification_failed")):
            with self.subTest(kw=kw):
                self.assertEqual(await self.refused(await self.pay(accepted, **kw)), code)
        self.node.blocks.clear()
        self.assertEqual(await self.refused(encode({"order": accepted["extra"]["order"],
                                                    "block": BLOCK})), "verification_failed")
        self.node.down = True
        self.assertEqual(await self.refused(await self.pay(accepted)), "verification_unavailable")

    async def test_an_expired_challenge_is_refused(self) -> None:
        credential = await self.pay(await self.accepted())
        self.clock.now = NOW + timedelta(seconds=600) + nano.PRESENT_GRACE + timedelta(seconds=1)
        self.assertEqual(await self.refused(credential), "invalid_challenge")
        self.clock.now = NOW + timedelta(seconds=600) + nano.PRESENT_GRACE   # still presentable
        await self.rail.verify(credential, offer())

    async def test_the_reconciler_reads_the_block(self) -> None:
        credential = await self.pay(await self.accepted())
        verified = await self.rail.verify(credential, offer())
        payment = Unconfirmed(proof_id=verified.proof_id, payer=PAYER, amount_micro=50_000,
                              asset="xno", pay_to=PAY_TO, created_at=NOW.replace(tzinfo=None),
                              expires_at=verified.expires_at)
        self.assertEqual((await self.rail.find(payment)).state, "landed")
        self.node.blocks[BLOCK]["contents"]["link_as_account"] = OTHER
        self.assertEqual((await self.rail.find(payment)).state, "absent")
        self.node.blocks[BLOCK]["confirmed"] = "false"
        self.assertEqual((await self.rail.find(payment)).state, "pending")
        self.node.down = True
        self.assertEqual((await self.rail.find(payment)).state, "pending")


NANO_ENV = {"TREG_AGENT_PAYMENTS_ENABLED": "1", "TREG_AGENT_PAYMENTS_SECRET": SECRET,
            "TREG_AGENT_PAYMENTS_NANO_ENABLED": "1", "TREG_AGENT_PAYMENTS_NANO_PAY_TO": PAY_TO,
            "TREG_AGENT_PAYMENTS_NANO_NODE_URL": "https://node.example",
            "TREG_AGENT_PAYMENTS_NANO_XNO_PER_USD": "1.25"}


@unittest.skipUnless(importlib.util.find_spec("pydantic_settings"), "needs the server extra")
class Settings(unittest.TestCase):
    def problems(self, **env: str) -> list[str]:
        from treg.config import Settings as TregSettings

        values = {**NANO_ENV, **env}
        with mock.patch.dict(os.environ, {k: v for k, v in values.items() if v is not None}):
            for name in [k for k, v in values.items() if v is None]:
                os.environ.pop(name, None)
            return TregSettings().agent_payments_problems()

    def test_a_complete_nano_configuration_starts(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_a_wrong_or_missing_nano_setting_is_named(self) -> None:
        self.assertIn("NANO_PAY_TO as a nano_ address", " ".join(self.problems(
            TREG_AGENT_PAYMENTS_NANO_PAY_TO=PAY_TO[:-1] + "1")))
        self.assertIn("NANO_XNO_PER_USD as a positive decimal", " ".join(self.problems(
            TREG_AGENT_PAYMENTS_NANO_XNO_PER_USD="0")))
        self.assertIn("TREG_AGENT_PAYMENTS_NANO_NODE_URL", self.problems(
            TREG_AGENT_PAYMENTS_NANO_NODE_URL=None))
        self.assertIn("Nano does not settle to Stripe", " ".join(self.problems(
            TREG_AGENT_PAYMENTS_SETTLEMENT="stripe", TREG_AGENT_PAYMENTS_STRIPE_SECRET_KEY="sk")))


if __name__ == "__main__":
    unittest.main()
