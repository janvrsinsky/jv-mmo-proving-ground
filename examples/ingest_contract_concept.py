#!/usr/bin/env python3
"""Ingest contract concept: one contract, two severities, over combat telemetry.

A clean-room re-creation of the *idea* behind the private proving-ground ingest
path, the one that carried 149,288,931 telemetry rows into an indexed Postgres
table at 99.90% completeness. It shares no code with that system. It was written
from the concept, it runs on the standard library alone, and it reads 21
synthetic rows shipped next to it.

Scope note, because it matters more than the demo: the real telemetry leg runs
the STRUCTURAL tier alone. A volley has no budget to bust, so it either parses
or it does not, which is exactly why telemetry was the right vehicle for the
scale leg: the same validation machinery, no balance bands, run at volume. The
drift tier runs where balance data actually lives, on the 39-ship roster. This
sample puts both tiers over telemetry-shaped rows because the *split* is the
idea worth handing over, not because the 149M run flagged drift. It did not.

The idea being demonstrated is the severity split:

  structural defect   The row cannot be trusted to mean anything, so it is
                      QUARANTINED with a named reason code. It is never
                      silently dropped. A quarantined row is a row somebody can
                      still go and read, which is the whole difference between
                      a quarantine and a delete.

  soft / drift        The row is legal and parses fine, but something about it
                      is suspicious. It LOADS, it stays queryable, and it
                      raises a drift flag, because a value that is legal but
                      wrong is a design conversation, not a parse error.

That split is not a style preference. It is forced by the data. A structural
defect is decidable from the row on its own, so it can be caught at the door.
A drift signal is usually only visible once the row sits next to its
neighbours: you cannot quarantine an airship for firing after it died, because
the only way to learn that it died is to read a different row. So the drift
pass here runs where the real one runs, as a query over what was already
loaded. Quarantine is a gate; drift is a question you can only ask downstream.

Scope, stated plainly:

  * Clean room. Written from the concept, not copied from the private build.
  * Synthetic. Every row here was invented for this file, as was Meridian.
  * Standard library only. No dependencies, no install, no network.
  * The checks below are purely structural: enum membership, key
    wellformedness, integer-ness, required fields, referential sanity,
    nonnegativity. The pricing and balance rules that the real contract also
    enforces are deliberately NOT in this sample, and neither is any constant
    they use. There are no bands here. The omission is the point.

Usage:

    python3 examples/ingest_contract_concept.py [ROWS.jsonl]
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Meridian's vocabulary. Invented for this project, and carried in data rather
# than in code, which is why the engine underneath can stay domain-free.
OUTCOMES = ("crit", "hit", "kill", "miss")
FACTIONS = ("Ashfall", "Cirran", "Concord", "Reavers")

# A hull id is a key, so it gets a key's treatment: exact shape or nothing.
HULL_ID = re.compile(r"^H-\d{4}$")

REQUIRED = ("battle_id", "event_id", "tick", "attacker_hid", "attacker_faction",
            "defender_hid", "defender_faction", "outcome")
INTEGER_FIELDS = ("battle_id", "event_id", "tick")


@dataclass
class Defect:
    code: str
    detail: str


@dataclass
class Report:
    read: int = 0
    loaded: List[Dict[str, Any]] = field(default_factory=list)
    quarantined: List[Tuple[Dict[str, Any], Defect]] = field(default_factory=list)
    drift: List[Tuple[Dict[str, Any], Defect]] = field(default_factory=list)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python, and a JSON true is not a tick.
    return isinstance(value, int) and not isinstance(value, bool)


def check_structural(row: Dict[str, Any]) -> Optional[Defect]:
    """Decide the row on its own. First defect wins: it is already untrustworthy."""
    for key in REQUIRED:
        if key not in row:
            return Defect("missing_field", key + " (absent)")

    for key in INTEGER_FIELDS:
        if not _is_int(row[key]):
            return Defect("non_integer_field", key + " " + repr(row[key]))

    if row["tick"] < 0:
        return Defect("negative_tick", "tick " + str(row["tick"]))

    for key in ("attacker_hid", "defender_hid"):
        if not isinstance(row[key], str) or not HULL_ID.match(row[key]):
            return Defect("malformed_hull_id", key + " " + repr(row[key]))

    if row["outcome"] not in OUTCOMES:
        return Defect("unknown_outcome", "outcome " + repr(row["outcome"]))

    for key in ("attacker_faction", "defender_faction"):
        if row[key] not in FACTIONS:
            return Defect("unknown_faction", key + " " + repr(row[key]))

    if row["attacker_hid"] == row["defender_hid"]:
        return Defect("self_engagement", "both hulls are " + row["attacker_hid"])

    return None


def check_soft_row(row: Dict[str, Any]) -> List[Defect]:
    """Legal rows that still deserve a second look. Detectable in isolation."""
    if row["attacker_faction"] == row["defender_faction"]:
        return [Defect("same_faction_engagement",
                       row["attacker_faction"] + " engaged " + row["defender_faction"])]
    return []


def check_soft_store(loaded: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Defect]]:
    """Drift only a loaded store can see: an actor moving after its own death.

    This is the pass that argues for the whole design. The row is well formed,
    so nothing at the door could have rejected it. It becomes suspicious only
    once it is queryable, next to the row that killed it.
    """
    death: Dict[Tuple[int, str], int] = {}
    for row in loaded:
        if row["outcome"] == "kill":
            key = (row["battle_id"], row["defender_hid"])
            death[key] = min(death.get(key, row["tick"]), row["tick"])

    found: List[Tuple[Dict[str, Any], Defect]] = []
    for row in loaded:
        died_at = death.get((row["battle_id"], row["attacker_hid"]))
        if died_at is not None and row["tick"] > died_at:
            found.append((row, Defect(
                "posthumous_actor",
                row["attacker_hid"] + " acts at tick " + str(row["tick"])
                + ", died at " + str(died_at))))
    return found


def ingest(path: str) -> Report:
    report = Report()
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            report.read += 1
            # The defect detail is written here rather than taken from the
            # decoder's own message: that text varies by Python version, and a
            # reason code a reader can diff has to mean the same thing on every
            # machine that runs this.
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                report.quarantined.append(
                    ({}, Defect("unparseable_row", "line " + str(lineno) + ", not JSON")))
                continue

            if not isinstance(row, dict):
                report.quarantined.append(
                    ({}, Defect("unparseable_row",
                                "line " + str(lineno) + ", not a JSON object")))
                continue

            defect = check_structural(row)
            if defect is not None:
                report.quarantined.append((row, defect))
                continue

            report.loaded.append(row)
            for soft in check_soft_row(row):
                report.drift.append((row, soft))

    report.drift.extend(check_soft_store(report.loaded))
    report.drift.sort(key=lambda pair: (pair[0]["battle_id"], pair[0]["event_id"]))
    return report


RULE = "=" * 78
THIN = "-" * 78
CODE_COL = 24


def _row(battle: str, event: str, code: str, detail: str) -> str:
    return ("   " + battle.rjust(6) + "   " + event.rjust(5)
            + "   " + code.ljust(CODE_COL) + " " + detail)


def _table(rows: List[Tuple[Dict[str, Any], Defect]]) -> None:
    print(_row("battle", "event", "reason", "offending value"))
    for row, defect in rows:
        print(_row(str(row.get("battle_id", "?")), str(row.get("event_id", "?")),
                   defect.code, defect.detail))


def _by_reason(rows: List[Tuple[Dict[str, Any], Defect]]) -> None:
    counts: Dict[str, int] = {}
    for _row, defect in rows:
        counts[defect.code] = counts.get(defect.code, 0) + 1
    for code in sorted(counts):
        print("   " + (code + " ").ljust(CODE_COL + 8, ".")
              + " " + str(counts[code]).rjust(2))


def _contract() -> None:
    print(" CONTRACT")
    print("   outcome ......... " + " | ".join(OUTCOMES))
    print("   faction ......... " + " | ".join(FACTIONS))
    print("   hull id ......... H-#### exactly")
    print("   required ........ all 8 fields present")
    print("   integers ........ battle_id, event_id, tick (tick nonnegative)")
    print("   referential ..... attacker_hid is not defender_hid")
    print("")
    print("   No band, budget, or tuning rule appears above. The real contract")
    print("   carries those too; this sample deliberately does not.")


def render(report: Report, path: str) -> None:
    complete = (100.0 * len(report.loaded) / report.read) if report.read else 0.0

    print(RULE)
    print(" INGEST CONTRACT CONCEPT | MMO Airship Proving Ground")
    print(" clean-room re-creation, synthetic rows, standard library only")
    print(RULE)
    print("")
    _contract()
    print("")
    print(THIN)
    print("")
    print("   source .............. " + os.path.basename(path))
    print("   rows read ........... " + str(report.read))
    print("   loaded .............. " + str(len(report.loaded)))
    print("   quarantined ......... " + str(len(report.quarantined)))
    print("   drift-flagged ....... " + str(len(report.drift)))
    print("   completeness ........ " + format(complete, ".2f") + "%")
    print("")
    print(THIN)
    print(" QUARANTINED | structural defect, decidable from the row alone")
    print("   The row cannot be trusted to mean anything, so it does not load.")
    print("   It is held under a named reason code. Nothing is silently dropped.")
    print(THIN)
    _table(report.quarantined)
    print("")
    print(" by reason")
    _by_reason(report.quarantined)
    print("")
    print(THIN)
    print(" DRIFT FLAGGED | legal, loaded, still queryable")
    print("   These rows are well formed, so no gate could have refused them.")
    print("   posthumous_actor is the case that argues for the design: the row is")
    print("   clean, and the row that killed the actor is a different row, so the")
    print("   defect exists only once both are in the store. You cannot quarantine")
    print("   what you can only see downstream, so it loads and a query flags it.")
    print(THIN)
    _table(report.drift)
    print("")
    print(" by reason")
    _by_reason(report.drift)
    print("")
    print(THIN)
    print(" Every structural reason code fires exactly once here: this fixture is")
    print(" defect-dense on purpose, which is why completeness reads 62%. The real")
    print(" telemetry run read 99.90% over 149,288,931 rows, and it ran the")
    print(" structural tier alone: a volley has no budget to bust, so it either")
    print(" parses or it does not. That is what made telemetry the right vehicle")
    print(" for the scale leg. The drift tier runs where the balance data is.")
    print(RULE)


def main(argv: List[str]) -> int:
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_rows.jsonl")
    path = argv[1] if len(argv) > 1 else default
    if not os.path.isfile(path):
        print("error: no such file: " + path)
        return 2
    render(ingest(path), path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
