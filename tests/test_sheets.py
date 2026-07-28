"""Reading the response sheet and matching form questions."""

from conftest import make_response

from intake import sheets

def test_question_wording_differences_are_ignored():
    """Punctuation and casing changes to the form must not break matching."""
    response = sheets.FormResponse(2, {"What's your Discord handle?": "jordanr"})
    assert response.answer_to("Whats your discord handle") == "jordanr"


def test_curly_and_straight_apostrophes_match_each_other():
    """Google Forms stores a curly apostrophe; people type a straight one."""
    response = sheets.FormResponse(2, {"What’s your Discord handle?": "jordanr"})  # noqa: RUF001
    assert response.answer_to("What's your Discord handle?") == "jordanr"


def test_name_does_not_match_username():
    """The old substring matching read "Discord username" as the Name field."""
    response = make_response()
    assert response.answer_matching("name") == "Jordan Reyes"


def test_matching_skips_questions_with_no_answer():
    response = sheets.FormResponse(2, {"Your name": "", "Their name": "Alex"})
    assert response.answer_matching("name") == "Alex"


def test_matching_can_skip_questions_already_used():
    response = make_response()
    first = response.question_matching("name")
    second = response.question_matching("name", skip={first})
    assert first != second


def test_unknown_question_returns_empty_string():
    assert make_response().answer_to("A question nobody asked") == ""

def test_the_same_submission_always_fingerprints_the_same():
    assert make_response(4).fingerprint == make_response(4).fingerprint


def test_different_submissions_fingerprint_differently():
    assert make_response(4).fingerprint != make_response(5).fingerprint


def test_editing_an_answer_keeps_the_same_fingerprint():
    """Google Forms rewrites the row in place when someone edits a response.

    The fingerprint is built from who submitted and when, so an edit must not
    look like a brand new submission and produce a second ticket.
    """
    original = make_response(4)
    edited = make_response(4, {"Please describe what you need help with in detail": "Rewritten."})
    assert original.fingerprint == edited.fingerprint


def test_row_number_is_not_part_of_the_fingerprint():
    """Deleting a row above shifts everything up; that must not replay tickets."""
    response = make_response(4)
    moved = make_response(4)
    moved.row_number = 2
    assert response.fingerprint == moved.fingerprint

def test_blank_rows_are_skipped():
    values = [["Timestamp", "Name"], ["", ""], ["2026-07-28", "Jordan"]]
    responses = sheets._build_responses(values)
    assert len(responses) == 1
    assert responses[0].row_number == 3  # row 1 is the header, row 2 was blank


def test_duplicate_question_text_is_disambiguated():
    """Google Forms allows two questions with identical text."""
    headers = sheets._build_headers(["Name", "Name", "Name"])
    assert headers == ["Name", "Name (2)", "Name (3)"]


def test_unnamed_columns_get_a_placeholder():
    assert sheets._build_headers(["Timestamp", "  "]) == ["Timestamp", "Question 2"]


def test_short_rows_are_padded():
    """Sheets omits trailing empty cells, so rows can be shorter than headers."""
    values = [["Timestamp", "Name", "Notes"], ["2026-07-28", "Jordan"]]
    response = sheets._build_responses(values)[0]
    assert response.answers["Notes"] == ""


def test_an_empty_sheet_produces_no_responses():
    assert sheets._build_responses([]) == []
