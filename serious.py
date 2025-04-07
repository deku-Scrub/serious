#!/usr/bin/env python3
import heapq
import subprocess
import itertools
import csv
import time
import dataclasses
import math
import sqlite3
import argparse
import os
import pathlib


DEFAULT_DECK = 'default'


def _get_config_dir():
    # Taken from https://stackoverflow.com/questions/3250164/loading-a-config-file-from-operation-system-independent-place-in-python/63699709#63699709.
    config = os.environ.get('APPDATA') or os.environ.get('XDG_CONFIG_HOME')
    config = config if config else str(pathlib.Path.home() / '.config')
    return os.path.join(config, 'serious')


def _get_cmdline_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Serious is a spaced repetition program.

Use the `add` subcommand to add new cards as csv files.  Afterwards, to review with the default settings, call the program without any arguments.

Cards are saved to an SQLite database in the user's config directory, under the `serious` subdirectory.  This can be changed with `--db-path`.

If the `SERIOUS_TTS` environment variable is not empty, then it is assumed to be the name of a text-to-speech program, and questions and answers are piped to it.

Each card is reviewed at time intervals `t` (in hours) according to

    `t = exp(min(n, r) * ln(h + 1) / r) - 1`,

where `n` is the number of consecutive, successfully recalled reviews of a given card, `h` is the maximum number of hours between consecutive reviews of the same card, and `r` is the number of consecutive, successfully recalled reviews of the same card such that its next review occurs in `h` hours.  The parameters `h` and `r` can be customized with, respectively, `--hours-param` and `--reviews-param`.  If a card is forgotten, its number of consecutive, successfully recalled reviews is halved using integer division.
    """,
    )

    default_db = os.path.join(_get_config_dir(), 'serious.db')
    parser.add_argument(
        '--db-path',
        type=str,
        default=default_db,
        help=f'Path to database.  Default is `{default_db}`.',
    )

    parser.add_argument(
        '--decks',
        type=str,
        default='',
        help='Comma-separated list of decks to review.  Default is to review all decks.',
    )
    parser.add_argument(
        '--show-intervals',
        action='store_true',
        default=False,
        help='Show intervals at which cards are reviewed.  Units are in seconds.',
    )
    parser.add_argument(
        '--reviews-param',
        type=int,
        default=20,
        help='Paramater of the spacing algorithm.  Denotes the number of consecutive, successfully recalled reviews of a card so that the next review occurs in `--hours-param` hours.  Default is 20.',
    )
    parser.add_argument(
        '--hours-param',
        type=int,
        default=24 * 90,
        help='Paramater of the spacing algorithm.  Denotes the maximum number of hours between reviews of the same card.  Default is `24 * 90` hours.',
    )

    subparsers = parser.add_subparsers()

    addentry_parser = subparsers.add_parser('add')
    addentry_parser.add_argument(
        'filenames',
        metavar='filenames',
        type=str,
        nargs='+',
        default='',
        help='Files to add.  Each should be in comma-separated values format, with the question in the first field, and answer in the second.  The delimiter can be changed with `--delimiter`.',
    )
    addentry_parser.add_argument(
        '--deck',
        type=str,
        default=DEFAULT_DECK,
        help='The deck in which to add all items.  Defaults to the `{}` deck.'.format(
            DEFAULT_DECK
        ),
    )
    addentry_parser.add_argument(
        '-d',
        '--delimiter',
        type=str,
        default=',',
        help='Delimiter',
    )

    args = parser.parse_args()
    args.decks = list(filter(None, args.decks.strip().split(',')))
    return args


@dataclasses.dataclass(frozen=True)
class Item:
    rowid: int = 0
    review_time: int = 0
    trial: int = 0

    def __lt__(self, b):
        return self.review_time < b.review_time


@dataclasses.dataclass(frozen=True)
class ReviewItem:
    item: Item
    recalled: int
    forgot: int
    question: str
    answer: str
    history: str


def add_success(item, intervals):
    return _update_items(
        item,
        intervals,
        min(len(intervals) - 1, item.trial + 1),
    )


def add_failure(item, intervals):
    return _update_items(item, intervals, item.trial // 2)


def _update_items(item, intervals, trial):
    return dataclasses.replace(
        item,
        trial=trial,
        review_time=int(time.time() + intervals[trial]),
    )


def compute_intervals(trials, hours):
    c = math.log(hours + 1) / trials
    return [(math.exp(n * c) - 1) * 60 * 60 for n in range(trials + 1)]


def load_items(db, decks):
    where_clause = 'WHERE {}'.format('OR'.join(['(deck = ?)' for _ in decks]))
    with db:
        items = list(
            itertools.starmap(
                Item,
                db.execute(
                    'SELECT rowid, review_time, trial FROM Item {}'.format(
                        where_clause if decks else '',
                    ),
                    decks,
                ),
            )
        )
        heapq.heapify(items)
        return items


def update_item(item, db, success=True):
    with db:
        db.execute(
            """
            UPDATE Item
            SET
                review_time = ?,
                trial = ?,
                {recalled} = {recalled} + 1,
                history = history || {trial_success}
            WHERE rowid = ?
            """.format(
                recalled='recalled' if success else 'forgot',
                trial_success='"o"' if success else '"x"',
            ),
            (item.review_time, item.trial, item.rowid),
        )


def make_review_item(item, db):
    with db:
        row = db.execute(
            """
            SELECT
                recalled, forgot, question, answer, history
            FROM Item
            WHERE rowid = ?
            """,
            (item.rowid,),
        ).fetchone()

        return ReviewItem(
            item,
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
        )


def make_db(filename):
    db = sqlite3.connect(filename)
    with db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS Item (
                recalled INT DEFAULT 0,
                forgot INT DEFAULT 0,
                review_time INT DEFAULT 0,
                trial INT DEFAULT 0,
                question VARCHAR UNIQUE,
                answer VARCHAR,
                history VARCHAR DEFAULT "",
                deck VARCHAR DEFAULT "{}"
                )
            """.format(DEFAULT_DECK)
        )
        db.execute('CREATE INDEX IF NOT EXISTS Item_deck ON Item (deck)')
        return db


def _insert_into_item(db, batch):
    with db:
        db.executemany(
            'INSERT INTO Item (question, answer, deck) VALUES (?, ?, ?)',
            (batch),
        )


def add_items(db, deck, csv_rows, batch_size=1000):
    batch = []
    for row in csv_rows:
        batch.append(row + [deck])
        if len(batch) >= batch_size:
            _insert_into_item(db, batch)
            batch = []
    if batch:
        _insert_into_item(db, batch)
        batch = []


def _prompt(prompt, tts_text):
    print(prompt, end='')
    if tts_program := os.environ.get('SERIOUS_TTS', ''):
        subprocess.run(tts_program, input=tts_text, text=True, shell=True)
    return input()


def review(review_item):
    status = '{recalled}/{n_trials} {history}'.format(
        recalled=review_item.recalled,
        n_trials=review_item.recalled + review_item.forgot,
        history=(review_item.history[-5:][::-1] + '-----')[:5],
    )
    q_prompt = f'{status}\nQ: {review_item.question}\nreveal [a]nswer, [q]uit: '
    ans_prompt = f'A: {review_item.answer}\n[r]ecalled, [f]orgot: '
    while True:
        x = _prompt(q_prompt, review_item.question)
        if x == 'q':
            return None
        if x != 'a':
            continue
        while True:
            y = _prompt(ans_prompt, review_item.answer)
            if y == 'r':
                return True
            elif y == 'f':
                return False


def add_items_from_files(db, args):
    for csv_filename in args.filenames:
        try:
            with open(csv_filename) as fis:
                add_items(db, args.deck, csv.reader(fis, delimiter=args.delimiter))
        except sqlite3.IntegrityError:
            print(f'Duplicate question in file `{csv_filename}`')


def start_review(items, db, intervals, args):
    while items and (items[0].review_time <= time.time()):
        item = heapq.heappop(items)
        if (result := review(make_review_item(item, db))) == True:
            item = add_success(item, intervals)
            update_item(item, db)
        elif result == False:
            item = add_failure(item, intervals)
            update_item(item, db, success=False)
        else:
            break
        heapq.heappush(items, item)
        print()


def main():
    os.makedirs(_get_config_dir(), exist_ok=True)

    args = _get_cmdline_args()
    db = make_db(args.db_path)
    intervals = compute_intervals(args.reviews_param, args.hours_param)

    if args.show_intervals:
        print(intervals)
        return
    if 'filenames' in vars(args):
        add_items_from_files(db, args)
        return

    if items := load_items(db, args.decks):
        start_review(items, db, intervals, args)
        print('Next review scheduled for {}.'.format(time.ctime(items[0].review_time)))
    else:
        print(f'No cards scheduled for review.  Use the `add` sub-command to add some.')


if __name__ == '__main__':
    main()
