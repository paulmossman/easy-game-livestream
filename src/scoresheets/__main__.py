"""Prints a game's goals as they change, until the game is over.

From the repo root: ``python3 -m src.scoresheets <url or mock .yml>``, or
``ops scoresheet <url or mock .yml>``.
"""

import argparse
import time
from datetime import datetime

from . import DEFAULT_SETTLE_SECONDS, Goal, ScoresheetError, Strength, open_feed


def log(text: str) -> None:
    print(f'{datetime.now():%H:%M:%S} {text}', flush=True)


def describe(goal: Goal) -> str:
    assists = ', '.join(f'#{p.jersey} {p.first_name} {p.last_name}' for p in goal.assists) or 'unassisted'
    tags = [label for label, on in (('PP', goal.strength == Strength.POWER_PLAY),
                                    ('SHG', goal.strength == Strength.SHORTHANDED),
                                    ('EN', goal.empty_net)) if on]
    return (f'P{goal.period} {goal.clock} {goal.team}: #{goal.scorer.jersey} '
            f'{goal.scorer.first_name} {goal.scorer.last_name} ({assists}) {" ".join(tags)}').rstrip()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog='scoresheets',
        description="Print a game's goals as they change, until the game is over.")
    parser.add_argument('url', help='gamesheetstats.com game URL, or a mock game .yml file')
    parser.add_argument('--api-key', help='Firestore API key, if GameSheet needs one')
    parser.add_argument('--interval', type=float, default=5, metavar='SECONDS',
                        help='seconds between polls (default: %(default)s)')
    parser.add_argument('--settle', type=float, default=DEFAULT_SETTLE_SECONDS, metavar='SECONDS',
                        help='seconds without changes before a goal is settled (default: %(default)s)')
    parser.add_argument('--once', action='store_true', help='print the goals so far and exit')
    args = parser.parse_args()

    try:
        feed = open_feed(args.url, settle_seconds=args.settle, api_key=args.api_key)
    except (ScoresheetError, ValueError) as e:
        parser.exit(1, f'{parser.prog}: {e}\n')
    try:
        while True:
            try:
                for change in feed.poll():
                    log(f'{change.kind.value:8} {describe(change.goal)}')
            except ScoresheetError as e:
                if args.once:
                    parser.exit(1, f'{parser.prog}: {e}\n')
                log(f'error    {e}')
            if args.once:
                break
            if feed.game_over and feed.all_settled:
                log('game over')
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


main()
