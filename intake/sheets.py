"""Reading form responses out of the Google Sheet.

The rest of the bot works with `FormResponse` objects rather than raw rows, so
it never has to care that the sheet is really a grid of strings, or that people
reword the form questions from year to year.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import os
import re

import gspread
import requests

from . import config

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30

# Looked for in the working directory when GOOGLE_SERVICE_ACCOUNT_FILE is unset.
DEFAULT_CREDENTIAL_FILENAMES = ("credentials.json", "service_account.json")

_cached_client: gspread.Client | None = None

# Straight and curly apostrophes, plus a backtick. Google Forms usually stores
# the curly one while people type the straight one into .env.
APOSTROPHES = re.compile(r"['‘’`]")  # noqa: RUF001 - the curly ones are the point


def normalise(text: str) -> str:
    """Reduce a question to lowercase words separated by single spaces.

    Apostrophes are deleted rather than turned into spaces, so "What's your
    handle" and "Whats your handle" come out the same. Treating them as
    separators would split "what's" into "what s" and quietly stop the two
    spellings from matching.
    """
    without_apostrophes = APOSTROPHES.sub("", text.strip().lower())
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_apostrophes)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """True if `phrase` appears in `haystack` as whole words."""
    normalised_phrase = normalise(phrase)
    if not normalised_phrase:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(normalised_phrase)}(?![a-z0-9])", haystack) is not None


class FormResponse:
    """One row of the response sheet, with the header row as question names."""

    def __init__(self, row_number: int, answers: dict[str, str]):
        self.row_number = row_number
        self.answers = answers
        self._normalised = {question: normalise(question) for question in answers}

    def __repr__(self) -> str:
        return f"<FormResponse row={self.row_number} questions={len(self.answers)}>"

    @property
    def questions(self) -> list[str]:
        return list(self.answers)

    @property
    def fingerprint(self) -> str:
        """A stable ID for this submission, used to avoid duplicate tickets.

        Google Forms lets people edit a response after submitting, which
        rewrites the row in place. So we fingerprint *who* submitted and
        *when*, not the whole row: an edited response keeps its old
        fingerprint and does not produce a second ticket.
        """
        submitted_at = self.answer_matching("timestamp")
        submitter = self.answer_matching("discord id", "discord handle", "discord", "email")
        seed = f"{submitted_at}|{submitter}" if submitted_at else "|".join(self.answers.values())
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    def answer_to(self, question: str) -> str:
        """The answer to one exact question, ignoring punctuation differences."""
        wanted = normalise(question)
        for candidate, normalised in self._normalised.items():
            if normalised == wanted:
                return self.answers[candidate].strip()
        return ""

    def question_matching(self, *phrases: str, skip: set[str] | None = None) -> str | None:
        """The first question containing any of these phrases and holding an answer."""
        skip = skip or set()
        for question, normalised in self._normalised.items():
            if question in skip or not self.answers[question].strip():
                continue
            if any(_contains_phrase(normalised, phrase) for phrase in phrases):
                return question
        return None

    def answer_matching(self, *phrases: str, skip: set[str] | None = None) -> str:
        """The answer to the first question containing any of these phrases."""
        question = self.question_matching(*phrases, skip=skip)
        return self.answers[question].strip() if question else ""

    def questions_matching(self, *phrases: str) -> list[str]:
        """Every question containing any of these phrases, answered or not."""
        return [
            question
            for question, normalised in self._normalised.items()
            if any(_contains_phrase(normalised, phrase) for phrase in phrases)
        ]

    def first_answered(self, *questions: str) -> str:
        """The answer to whichever of these exact questions was filled in first."""
        for question in questions:
            if answer := self.answer_to(question):
                return answer
        return ""

def _build_headers(header_row: list[str]) -> list[str]:
    """Name each column, filling in blanks and disambiguating repeats.

    Google Forms happily allows two questions with identical text, and a
    dictionary can only hold one of them, so the second becomes "Question (2)".
    """
    times_seen: dict[str, int] = {}
    headers: list[str] = []
    for index, raw_header in enumerate(header_row):
        name = raw_header.strip() or f"Question {index + 1}"
        times_seen[name] = times_seen.get(name, 0) + 1
        count = times_seen[name]
        headers.append(f"{name} ({count})" if count > 1 else name)
    return headers


def _build_responses(values: list[list[str]]) -> list[FormResponse]:
    """Convert the raw sheet grid into responses, skipping blank rows."""
    if not values:
        return []

    headers = _build_headers(values[0])
    responses = []
    # Sheet rows are 1-based and row 1 is the header, so data starts at row 2.
    for row_number, row in enumerate(values[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        answers = {
            header: (row[index] if index < len(row) else "")
            for index, header in enumerate(headers)
        }
        responses.append(FormResponse(row_number, answers))
    return responses

def _service_account_path() -> str:
    """Path to the Google service account key, or "" if there is not one."""
    if config.SERVICE_ACCOUNT_FILE:
        return config.SERVICE_ACCOUNT_FILE
    for filename in DEFAULT_CREDENTIAL_FILENAMES:
        if os.path.exists(filename):
            return filename
    return ""


def _get_client(credentials_path: str) -> gspread.Client:
    """Authenticate with Google once and reuse the connection afterwards."""
    global _cached_client
    if _cached_client is None:
        log.info("Authenticating with Google Sheets using %s", credentials_path)
        _cached_client = gspread.service_account(filename=credentials_path)
    return _cached_client


def _fetch_with_service_account(credentials_path: str) -> list[list[str]]:
    client = _get_client(credentials_path)
    spreadsheet = client.open_by_key(config.SHEET_ID)
    worksheet = spreadsheet.worksheet(config.SHEET_TAB) if config.SHEET_TAB else spreadsheet.sheet1
    return worksheet.get_all_values()


def _fetch_public_csv() -> list[list[str]]:
    """Read the sheet through its public CSV export.

    Only works when the sheet is shared with "anyone with the link", which
    means the submissions are readable by anyone who knows the sheet ID.
    """
    gid = config.SHEET_GID or "0"
    urls = [
        f"https://docs.google.com/spreadsheets/d/{config.SHEET_ID}/gviz/tq?tqx=out:csv&gid={gid}",
        f"https://docs.google.com/spreadsheets/d/{config.SHEET_ID}/export?format=csv&gid={gid}",
    ]
    errors = []
    for url in urls:
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return list(csv.reader(io.StringIO(response.text)))
        except Exception as error:
            errors.append(f"{url.split('/')[-1]}: {error}")
    raise RuntimeError("Could not read the sheet as public CSV -> " + "; ".join(errors))


def _fetch_values() -> list[list[str]]:
    """Read the whole response sheet. Blocking, so callers use a worker thread."""
    if not config.SHEET_ID:
        raise RuntimeError("INTAKE_SHEET_ID is not set")

    if credentials_path := _service_account_path():
        return _fetch_with_service_account(credentials_path)

    if config.ALLOW_PUBLIC_CSV:
        return _fetch_public_csv()

    raise RuntimeError(
        "No Google credentials found. Set GOOGLE_SERVICE_ACCOUNT_FILE (recommended), "
        "or set INTAKE_ALLOW_PUBLIC_CSV=1 to read a link-shared sheet instead."
    )


async def fetch_responses() -> list[FormResponse]:
    """Every response currently in the sheet, oldest first."""
    values = await asyncio.to_thread(_fetch_values)
    return _build_responses(values)
