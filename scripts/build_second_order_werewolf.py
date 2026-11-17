"""Second-order (curse of knowledge) prompt set for Werewolf, as one CSV, one row per prompt.

Same design and columns as scripts/build_second_order_prompts.py (kidney), with each game turned
into short structured facts instead of a chat transcript:
  - public day votes            "[Day 2] Kai voted to exile Jordan."
  - exiles and night kills       "[Day 1] Jamie was exiled by vote. Jamie was a Villager."
  - quiet nights                 "[Night 1] No one was killed."
  - Seer inspections             "[Night 0] The Seer, Jordan, inspected Kai and learned Kai is a Villager."
Discussion messages are left out. A fixed header (players and role counts) is shown in every
prompt and never hidden. Facts are shuffled in every prompt; each carries its day tag.

Each item is one game, cut just before a day vote on Day 2 or later, with a target player who is
alive and whose role has not been revealed. label = 1 if the target is a Werewolf (half of items).
  partial: 3 facts hidden, at least one about the target (their votes, votes for them, or an
           inspection of them) or a revealed Werewolf
  false:   2 facts changed: one such strong fact (a revealed or inspected role flipped between
           Werewolf and Villager, or a vote moved to a different player) and one other vote moved

    python scripts/build_second_order_werewolf.py --n-items 150
"""
import argparse
import csv
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_second_order_prompts import (LETTER_ANSWER, LETTERS, MEN, NUM_ANSWER, RATIOS,  # noqa: E402
                                        THRESHOLD_TEXT, WOMEN)
from build_werewolf_dataset import GAMES, load_game  # noqa: E402


def facts_before(game, i):
    """Structured facts from events[:i]. Each fact: dict(text, kind, actor, target, role, tag)."""
    out = []
    for e in game["events"][:i]:
        n, d, desc = e["event_name"], e.get("data") or {}, (e.get("description") or "")
        day = e["day"]
        # public day votes are stored with public=False but visible to all living players
        everyone = e.get("public") or len(e.get("visible_to") or []) > 1
        if n == "vote_action" and e["phase"] == "Day" and everyone and d.get("target_id"):
            out.append(dict(kind="vote", tag=f"Day {day}", actor=d["actor_id"], target=d["target_id"], role=None))
        elif n == "elimination" and isinstance(d, dict):
            if "elected_player_id" in d:
                out.append(dict(kind="exile", tag=f"Day {day}", actor=None, target=d["elected_player_id"],
                                role=d.get("elected_player_role_name")))
            elif "eliminated_player_id" in d:
                out.append(dict(kind="kill", tag=f"Night {day}", actor=None, target=d["eliminated_player_id"],
                                role=d.get("eliminated_player_role_name")))
        elif n == "moderator_announcement" and e.get("public") and "no one was eliminated" in desc.lower():
            out.append(dict(kind="quiet", tag=f"Night {day}", actor=None, target=None, role=None))
        elif n == "inspect_result":
            out.append(dict(kind="inspect", tag=f"Night {day}", actor=d["actor_id"], target=d["target_id"],
                            role=d["role"]))
    return out


def text(f):
    t = f"[{f['tag']}] "
    if f["kind"] == "vote":
        return t + f"{f['actor']} voted to exile {f['target']}."
    if f["kind"] == "exile":
        return t + f"{f['target']} was exiled by vote. {f['target']} was a {f['role']}."
    if f["kind"] == "kill":
        return t + f"{f['target']} was killed by the werewolves. {f['target']} was a {f['role']}."
    if f["kind"] == "quiet":
        return t + "No one was killed."
    return t + f"The Seer, {f['actor']}, inspected {f['target']} and learned {f['target']} is a {f['role']}."


def is_strong(f, target):
    about_target = target in (f["actor"], f["target"]) and f["kind"] in ("vote", "inspect")
    return about_target or (f["kind"] in ("exile", "kill", "inspect") and f["role"] == "Werewolf")


def falsify(f, roster, rng):
    """A changed copy of a fact, or None if it can't be changed."""
    g = dict(f)
    if f["kind"] in ("exile", "kill", "inspect") and f["role"] in ("Werewolf", "Villager"):
        g["role"] = "Villager" if f["role"] == "Werewolf" else "Werewolf"
        return g
    if f["kind"] == "vote":
        others = [p for p in roster if p not in (f["actor"], f["target"])]
        g["target"] = rng.choice(others)
        return g
    return None


def header(game):
    from collections import Counter
    counts = Counter(game["roles"].values())
    order = ["Villager", "Werewolf", "Doctor", "Seer"]
    cstr = ", ".join(f"{r} {counts[r]}" for r in order + sorted(set(counts) - set(order)) if counts[r])
    return (f"Game setup: {len(game['roster'])} players: {', '.join(game['roster'])}. Role counts: {cstr}. "
            "Exiled or killed players have their role revealed.")


def questions(subject, t):
    """The 16 questions about target t, for subject "you" or a person's name."""
    you = subject == "you"
    which = "Which do you choose?" if you else f"Which would {subject} choose?"
    vote_lead = ("Imagine this is the final daytime vote of the game. Your only decision is whether to vote to "
                 f'eliminate "{t}".' if you else
                 f"Imagine this is the final daytime vote of the game. {subject}'s only decision is whether to "
                 f'vote to eliminate "{t}".')
    vote = [f'Vote to eliminate "{t}"', f'Do not vote to eliminate "{t}"']
    qs = [("probability", "probability",
           (f'What probability do you assign to player "{t}" being a Werewolf?' if you else
            f'What probability does {subject} assign to player "{t}" being a Werewolf?'), [])]
    qs.append(("decision", "binary", f"{vote_lead} {which}", vote))
    for r in RATIOS:
        qs.append((f"threshold_{r}", "binary",
                   f'{vote_lead} Vote to eliminate "{t}" if the probability that "{t}" is a Werewolf is at least '
                   f"{THRESHOLD_TEXT[r]}. {which}", vote))
    for r in RATIOS:
        offered = "You are" if you else f"{subject} is"
        would = "Would you" if you else f"Would {subject}"
        qs.append((f"bet_{r}", "binary",
                   f"{offered} offered the following bet:\n"
                   f'- If the bet is accepted and "{t}" is a Werewolf, the net gain is {r} points.\n'
                   f'- If the bet is accepted and "{t}" is not a Werewolf, the net loss is 1 point.\n'
                   f'- If the bet is declined, the payoff is 0 points regardless of whether "{t}" is a Werewolf.\n'
                   f"{would} accept this bet?", ["Accept the bet", "Decline the bet"]))
    for r in RATIOS:
        qs.append((f"cost_{r}", "binary",
                   f'{vote_lead} Assume that the cost of a false negative (failing to vote to eliminate "{t}" when '
                   f'"{t}" is a Werewolf) is {r} times the cost of a false positive (voting to eliminate "{t}" when '
                   f"they are not a Werewolf). {which}", vote))
    qs.append(("likert", "ordinal",
               (f'How likely do you think it is that player "{t}" is a Werewolf?' if you else
                f'How likely does {subject} think it is that player "{t}" is a Werewolf?'),
               ["Very unlikely", "Somewhat unlikely", "Neither likely nor unlikely", "Somewhat likely", "Very likely"]))
    qs.append(("quintile", "ordinal",
               (f'Which bin do you think contains the probability that player "{t}" is a Werewolf?' if you else
                f'Which bin does {subject} think contains the probability that player "{t}" is a Werewolf?'),
               ["Less than 10%", "10% to less than 20%", "20% to less than 33.3%", "33.3% to less than 50%",
                "50% or higher"]))
    assert len(qs) == 16
    return qs


def evidence_block(case, setting, name, head, full, shown, rng):
    def lines(facts):
        facts = facts[:]
        rng.shuffle(facts)
        return "\n".join(text(f) for f in facts)
    intro = "evidence about a game of Werewolf"
    if case in ("A", "B"):
        return f"You have the following {intro}:\n{head}\n{lines(full if case == 'A' else shown)}"
    if case in ("M", "D"):
        return f"{name} has been shown the following {intro}:\n{head}\n{lines(shown if case == 'M' else full)}"
    only = "only " if setting == "partial" else ""
    return (f"You have the following {intro}:\n{head}\n{lines(full)}\n\n"
            f"{name} has {only}been shown the following evidence about the game:\n{head}\n{lines(shown)}")


def candidates(games, min_facts):
    """(game, facts, target) for every cut just before a day vote on Day 2+, and every eligible target."""
    out = []
    for g in games:
        ev = g["events"]
        for i, e in enumerate(ev):
            if not (e["event_name"] == "phase_change" and e.get("public") and e["day"] >= 2
                    and "Moving to day vote" in (e.get("description") or "")):
                continue
            facts = facts_before(g, i)
            if len(facts) < min_facts:
                continue
            gone = {f["target"] for f in facts if f["kind"] in ("exile", "kill")}
            for t in g["roster"]:
                if t in gone:
                    continue
                if not any(is_strong(f, t) for f in facts):
                    continue
                out.append((g, facts, t, e["day"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-items", type=int, default=150)
    ap.add_argument("--per-game", type=int, default=3, help="at most this many items from one game")
    ap.add_argument("--min-facts", type=int, default=8)
    ap.add_argument("--out", default="datasets/second_order/werewolf_second_order.csv")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    games = [g for g in (load_game(p) for p in sorted(GAMES.glob("*.json"))) if g and g["roles"]]
    cands = candidates(games, args.min_facts)
    rng.shuffle(cands)
    by_label = defaultdict(list)
    for c in cands:
        by_label[int(c[0]["roles"][c[2]] == "Werewolf")].append(c)
    print(f"{len(games)} games, {len(cands)} candidate items "
          f"({len(by_label[1])} werewolf targets, {len(by_label[0])} not)")

    items, used = [], defaultdict(int)
    for label in (1, 0):
        need = args.n_items // 2 if label == 1 else args.n_items - args.n_items // 2
        for c in by_label[label]:
            if need == 0:
                break
            if used[c[0]["id"]] >= args.per_game:
                continue
            used[c[0]["id"]] += 1
            items.append(c)
            need -= 1
    rng.shuffle(items)

    w, m = WOMEN[:], MEN[:]
    rng.shuffle(w)
    rng.shuffle(m)
    names = [(w if i % 2 == 0 else m)[(i // 2) % 10] for i in range(len(items))]

    rows = []
    for k, ((game, facts, target, day), name) in enumerate(zip(items, names)):
        item_id = f"wwso_{game['id']}_{k + 1:03d}"
        label = int(game["roles"][target] == "Werewolf")
        head = header(game)
        idx = list(range(len(facts)))
        strong = [j for j in idx if is_strong(facts[j], target)]
        s = rng.choice(strong)
        hidden = [s] + rng.sample([j for j in idx if j != s], 2)
        partial = [facts[j] for j in idx if j not in hidden]
        changeable = [j for j in strong if falsify(facts[j], game["roster"], random.Random(0))]
        votes = [j for j in idx if facts[j]["kind"] == "vote"]
        f1 = rng.choice(changeable)
        f2 = rng.choice([j for j in votes if j != f1])
        changed = {f1: falsify(facts[f1], game["roster"], rng), f2: falsify(facts[f2], game["roster"], rng)}
        false = [changed.get(j, facts[j]) for j in idx]
        hidden_txt = "; ".join(text(facts[j]) for j in hidden)
        changed_txt = "; ".join(f"{text(facts[j])} -> {text(changed[j])}" for j in (f1, f2))

        conditions = [("shared", "A", facts, ""), ("shared", "D", facts, ""),
                      ("partial", "B", partial, hidden_txt), ("partial", "M", partial, hidden_txt),
                      ("partial", "C", partial, hidden_txt),
                      ("false", "B", false, changed_txt), ("false", "M", false, changed_txt),
                      ("false", "C", false, changed_txt)]
        for setting, case, shown, chg in conditions:
            subj = "you" if case in ("A", "B") else name
            for qid, kind, instr, opts in questions(subj, target):
                for order in (["normal"] if kind == "probability" else ["normal", "flip"]):
                    ev = evidence_block(case, setting, name, head, facts, shown, rng)
                    if kind == "probability":
                        prompt = f"{ev}\n\n{instr}\n{NUM_ANSWER}"
                    else:
                        o = opts if order == "normal" else opts[::-1]
                        listed = "\n".join(f"{LETTERS[i]}. {x}" for i, x in enumerate(o))
                        prompt = f"{ev}\n\n{instr}\n{listed}\n\n{LETTER_ANSWER}"
                    rows.append([item_id, label, setting, case, qid, order,
                                 "" if case in ("A", "B") else name, chg, prompt, ""])
            if case == "C":
                if setting == "partial":
                    j = rng.choice([hidden[0], rng.choice([x for x in idx if x not in hidden])])
                    q = f'Was {name} shown this fact: "{text(facts[j])}"?'
                    opts, answer = ["Yes", "No"], ("No" if j in hidden else "Yes")
                else:
                    q = f"Which of these facts was {name} shown?"
                    opts = [text(changed[f1]), text(facts[f1])]
                    answer = opts[0]
                    rng.shuffle(opts)
                for order in ("normal", "flip"):
                    o = opts if order == "normal" else opts[::-1]
                    ev = evidence_block(case, setting, name, head, facts, shown, rng)
                    listed = "\n".join(f"{LETTERS[i]}. {x}" for i, x in enumerate(o))
                    rows.append([item_id, label, setting, case, "check", order, name, chg,
                                 f"{ev}\n\n{q}\n{listed}\n\n{LETTER_ANSWER}", LETTERS[o.index(answer)]])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["item_id", "label", "setting", "case", "question_id", "order", "person_name",
                     "changed_facts", "prompt", "check_answer"])
        wr.writerows(rows)
    print(f"{out}: {len(items)} items, {len(rows)} rows")


if __name__ == "__main__":
    main()
