#!/usr/bin/python3

"""
This script initialises Vega Markets.

1. Save market parameters from an existing network:
   curl -s https://node.example.com/markets >~/markets.json

2. Customise market parameters if needed.

3. Run this python script:
   python3 init-markets.py \
     --markets markets.json \
     --walletname "..." \
     --passphrase "..." \
     --walletserver https://wallet.example.com \
     --veganode node.example.com:3002
"""

import argparse
import json
import logging
import random
import requests
import string
import time
from typing import Any, Dict, List, Tuple
from google.protobuf.empty_pb2 import Empty

import vegaapiclient as vac


LOGGER = "init-markets"

MARKET_ID = "_market_id"
PROPOSAL_ID = "_proposal_id"
PROPOSAL_REF = "_ref"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialise markets")

    parser.add_argument(
        "--loglevel",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"],
        help="Log level",
    )

    parser.add_argument(
        "--markets",
        type=str,
        required=True,
        help=(
            "Either a path to a markets JSON file, or a URL to a Vega node's "
            "markets endpoint"
        ),
    )

    parser.add_argument(
        "--walletname",
        type=str,
        required=True,
        help="Wallet name",
    )

    parser.add_argument(
        "--passphrase",
        type=str,
        required=True,
        help="Wallet passphrase",
    )

    parser.add_argument(
        "--walletserver",
        type=str,
        required=True,
        help="wallet server (e.g. https://wallet.example.com)",
    )

    parser.add_argument(
        "--veganode",
        type=str,
        required=True,
        help="vega node (e.g. node.example.com:3333)",
    )

    return parser.parse_args()


def enum_to_str(e: Any, val: int) -> str:
    return e.keys()[e.values().index(val)]


def error_msg_from_response(msg: str, r: requests.Response) -> str:
    return f"Error: {msg} - HTTP {r.status_code} {r.text}"


def propose_markets(
    tradingcli: vac.VegaTradingClient,
    tradingdatacli: vac.VegaTradingDataClient,
    walletcli: vac.WalletClient,
    markets: List[Dict[str, Any]],
    pubkey: str,
) -> None:
    vegatime = int(tradingdatacli.GetVegaTime(Empty()).timestamp / 1e9)

    for market in markets:
        propose_market(
            tradingcli, tradingdatacli, walletcli, market, pubkey, vegatime
        )
        print(vegatime)


def propose_market(
    tradingcli: vac.VegaTradingClient,
    tradingdatacli: vac.VegaTradingDataClient,
    walletcli: vac.WalletClient,
    market: Dict[str, Any],
    pubkey: str,
    vegatime: int,
) -> None:

    logger = logging.getLogger(LOGGER)
    if (
        PROPOSAL_REF not in market
        or market[PROPOSAL_REF] is None
        or market[PROPOSAL_REF] == ""
    ):
        choices = string.ascii_letters + string.digits
        ref = "".join(random.choice(choices) for i in range(40))
        market[PROPOSAL_REF] = ref
    else:
        ref = market[PROPOSAL_REF]

    instr = market["tradableInstrument"]["instrument"]
    lnrm = market["tradableInstrument"]["logNormalRiskModel"]
    req = vac.api.trading.PrepareProposalRequest(
        partyID=pubkey,
        reference=ref,
        proposal=vac.governance.ProposalTerms(
            closingTimestamp=vegatime + 14,
            enactmentTimestamp=vegatime + 16,
            validationTimestamp=vegatime + 12,
            newMarket=vac.governance.NewMarket(
                changes=vac.governance.NewMarketConfiguration(
                    instrument=vac.governance.InstrumentConfiguration(
                        name=instr["name"],
                        code=instr["code"],
                        baseName=instr["baseName"],
                        quoteName=instr["quoteName"],
                        future=vac.governance.FutureProduct(
                            asset=instr["future"]["asset"],
                            maturity=instr["future"]["maturity"],
                        ),
                    ),
                    decimalPlaces=int(market["decimalPlaces"]),
                    metadata=[],
                    openingAuctionDuration=1,  # int64
                    logNormal=vac.markets.LogNormalRiskModel(
                        riskAversionParameter=lnrm["riskAversionParameter"],
                        tau=lnrm["tau"],
                        params=vac.markets.LogNormalModelParams(
                            mu=lnrm["params"]["mu"],
                            r=lnrm["params"]["r"],
                            sigma=lnrm["params"]["sigma"],
                        ),
                    ),
                    continuous=vac.markets.ContinuousTrading(
                        tickSize=market["continuous"]["tickSize"],
                    ),
                ),
            ),
        ),
    )
    tradingcli.prepare_sign_submit_tx(
        walletcli, pubkey, req, tradingcli.PrepareProposal
    )
    logger.info(f"Proposed market (ref {ref})")


def get_proposal_ids(
    tradingdatacli: vac.VegaTradingDataClient,
    markets: List[Dict[str, Any]],
    pubkey: str,
) -> None:
    logger = logging.getLogger(LOGGER)
    req = vac.api.trading.GetProposalsByPartyRequest(partyID=pubkey)
    while True:
        response = tradingdatacli.GetProposalsByParty(req)
        for datum in response.data:
            proposal = datum.proposal
            for mkt in markets:
                if PROPOSAL_ID in mkt:
                    continue
                ref = mkt[PROPOSAL_REF]
                if ref == proposal.reference:
                    if (
                        proposal.state
                        != vac.governance.Proposal.State.STATE_OPEN
                    ):
                        state_str = enum_to_str(
                            vac.governance.Proposal.State, proposal.state
                        )
                        reason = enum_to_str(
                            vac.governance.ProposalError, proposal.reason
                        )
                        raise Exception(
                            f"Proposal (ref {ref}) is in state {state_str}. "
                            f"Reason: {reason}"
                        )
                    mkt[PROPOSAL_ID] = proposal.ID
                    logger.info(f"Proposal {ref} -> {proposal.ID}")
        done = sum(PROPOSAL_ID in m for m in markets)
        logger.info(f"Getting proposal IDs. Progress: {done}/{len(markets)}")
        if all(PROPOSAL_ID in m for m in markets):
            break
        time.sleep(1)


def wallet_client(
    walletserver: str, walletname: str, passphrase: str
) -> Tuple[vac.WalletClient, str]:
    wc = vac.WalletClient(walletserver)
    r = wc.login(walletname, passphrase)
    if r.status_code != 200:
        raise Exception(error_msg_from_response("wallet: failed to log in", r))

    r = wc.listkeys()
    if r.status_code != 200:
        raise Exception(
            error_msg_from_response(
                f"wallet: failed to list keypairs for {walletname}", r
            )
        )

    keys = [keypair["pub"] for keypair in r.json()["keys"]]
    if len(keys) == 0:
        raise Exception(
            error_msg_from_response(f"wallet: no keypairs for {walletname}", r)
        )

    return wc, keys[0]


def vote_on_proposals(
    tradingcli: vac.VegaTradingClient,
    tradingdatacli: vac.VegaTradingDataClient,
    walletclient: vac.WalletClient,
    markets: List[Dict[str, Any]],
    pubkey: str,
):
    vegatime = int(tradingdatacli.GetVegaTime(Empty()).timestamp / 1e9)
    for market in markets:
        vote_on_proposal(tradingcli, walletclient, market, pubkey, vegatime)


def vote_on_proposal(
    tradingcli: vac.VegaTradingClient,
    walletclient: vac.WalletClient,
    market: Dict[str, Any],
    pubkey: str,
    vegatime: int,
):
    logger = logging.getLogger(LOGGER)
    req = vac.api.trading.PrepareVoteRequest(
        vote=vac.governance.Vote(
            partyID=pubkey,
            value=vac.governance.Vote.Value.VALUE_YES,
            proposalID=market[PROPOSAL_ID],
            timestamp=vegatime,
        ),
    )
    tradingcli.prepare_sign_submit_tx(
        walletclient, pubkey, req, tradingcli.PrepareVote
    )
    logger.info(f"Submitted vote YES for proposal {market[PROPOSAL_ID]}")


def wait_for_markets(
    tradingdatacli: vac.VegaTradingDataClient, markets: List[Dict[str, Any]]
):
    logger = logging.getLogger(LOGGER)
    req = Empty()
    while True:
        response = tradingdatacli.Markets(req)
        for market in response.markets:
            for m in markets:
                if MARKET_ID in m:
                    continue
                if market.id == m[PROPOSAL_ID]:
                    m[MARKET_ID] = market.id
                    logger.info(f"Market exists (ID {market.id})")

        done = sum(MARKET_ID in m for m in markets)
        logger.info(
            f"Waiting for market enactment. Progress: {done}/{len(markets)}"
        )
        if all(MARKET_ID in m for m in markets):
            break
        time.sleep(1)


def main():
    args = parse_args()
    logging.basicConfig(format="%(asctime)-15s %(levelname)s %(message)s")
    logger = logging.getLogger(LOGGER)
    logger.setLevel(getattr(logging, args.loglevel))

    tradingcli = vac.VegaTradingClient(args.veganode)
    tradingdatacli = vac.VegaTradingDataClient(args.veganode)

    walletcli, pubkey = wallet_client(
        args.walletserver, args.walletname, args.passphrase
    )
    logger.info(f"Using pubkey {pubkey}")

    if args.markets.startswith("http"):
        # URL
        markets = requests.get(args.markets).json()
    else:
        # path
        markets = json.load(open(args.markets))

    if isinstance(markets, dict) and "markets" in markets:
        markets = markets["markets"]
    assert isinstance(markets, list)

    propose_markets(tradingcli, tradingdatacli, walletcli, markets, pubkey)
    assert all(PROPOSAL_REF in mkt for mkt in markets)

    get_proposal_ids(tradingdatacli, markets, pubkey)
    assert all(PROPOSAL_ID in mkt for mkt in markets)

    vote_on_proposals(tradingcli, tradingdatacli, walletcli, markets, pubkey)

    wait_for_markets(tradingdatacli, markets)

    logger.info("Done")


if __name__ == "__main__":
    main()
