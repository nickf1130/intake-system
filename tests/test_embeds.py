"""Building the ticket embed, and staying inside Discord's limits.

Going over any limit makes Discord reject the whole message, which used to
mean the ticket channel was created but never posted into.
"""

from conftest import FakeUser, make_response

from intake import embeds, sheets


def build(response=None, **kwargs):
    options = {"department": "Competitive", "submitter_label": "<@1>", "team_names": []}
    options.update(kwargs)
    return embeds.build_ticket_embed(response or make_response(), **options)


def field_named(embed, name):
    return next((f for f in embed.fields if f.name == name), None)

def test_the_issue_is_the_headline_not_a_trailing_field():
    """It used to be the very last field, below every other answer."""
    embed = build()
    assert "scrim block" in embed.description


def test_a_missing_issue_still_produces_a_valid_embed():
    response = make_response(answers={"Please describe what you need help with in detail": ""})
    assert build(response).description


def test_routed_teams_are_listed():
    embed = build(team_names=["Valorant", "Rocket League"])
    assert field_named(embed, "Routed to").value == "Valorant, Rocket League"


def test_the_ticket_starts_open():
    assert "Open" in field_named(build(), embeds.STATUS_FIELD).value


def test_the_footer_points_back_at_the_sheet():
    embed = build(make_response(9))
    assert "Sheet row 9" in embed.footer.text


def test_only_the_relevant_affiliation_branch_is_shown():
    """The form asks different follow-ups depending on membership."""
    response = make_response(
        answers={
            "Are you affiliated with RIT Esports?": "Yes",
            "If you are not affiliated, how did you hear about us?": "A friend told me",
        }
    )
    rendered = " ".join(f"{f.name} {f.value}" for f in build(response).fields)
    assert "A friend told me" not in rendered


def test_the_unaffiliated_branch_shows_for_non_members():
    response = make_response(
        answers={
            "Are you affiliated with RIT Esports?": "No",
            "If you are not affiliated, how did you hear about us?": "A friend told me",
        }
    )
    rendered = " ".join(f"{f.name} {f.value}" for f in build(response).fields)
    assert "A friend told me" in rendered


def test_leftover_questions_are_grouped_not_one_field_each():
    extra = {f"Extra question {i}": f"Answer {i}" for i in range(8)}
    embed = build(make_response(answers=extra))
    assert len(embed.fields) < 8
    assert field_named(embed, "Additional details") is not None

def test_a_huge_form_stays_within_every_limit():
    """60 long questions used to blow past the 25-field limit and fail to send."""
    huge = sheets.FormResponse(3, {f"Question number {i}": "x" * 300 for i in range(60)})
    embed = build(huge)
    assert len(embed.fields) <= embeds.MAX_FIELDS
    assert len(embed) <= embeds.MAX_TOTAL


def test_dropped_answers_are_flagged_in_the_footer():
    """Staff need to know to go and read the sheet."""
    huge = sheets.FormResponse(3, {f"Question number {i}": "x" * 300 for i in range(60)})
    assert "see the sheet" in build(huge).footer.text


def test_an_over_long_answer_is_truncated_not_dropped():
    response = make_response(answers={"Please describe what you need help with in detail": "y" * 5000})
    embed = build(response)
    assert len(embed.description) <= embeds.MAX_DESCRIPTION
    assert embed.description.endswith("...")


def test_a_very_long_question_makes_a_valid_field_name():
    response = sheets.FormResponse(2, {"Q" * 500: "answer"})
    for embed_field in build(response).fields:
        assert len(embed_field.name) <= embeds.MAX_FIELD_NAME
        assert len(embed_field.value) <= embeds.MAX_FIELD_VALUE


def test_set_field_reports_when_something_did_not_fit():
    embed = embeds.discord.Embed(title="t")
    for i in range(embeds.MAX_FIELDS):
        assert embeds.set_field(embed, f"Field {i}", "value") is True
    assert embeds.set_field(embed, "One too many", "value") is False


def test_set_field_updates_rather_than_duplicates():
    embed = embeds.discord.Embed(title="t")
    embeds.set_field(embed, "Status", "Open")
    embeds.set_field(embed, "Status", "Claimed")
    assert len(embed.fields) == 1
    assert embed.fields[0].value == "Claimed"

def test_claiming_updates_status_and_colour():
    embed = embeds.mark_claimed(build(), FakeUser())
    assert embed.colour.value == embeds.COLOUR_CLAIMED
    assert field_named(embed, embeds.CLAIMED_FIELD) is not None


def test_closing_records_the_reason():
    embed = embeds.mark_closed(build(), FakeUser(), "sorted in DMs")
    assert embed.colour.value == embeds.COLOUR_CLOSED
    assert field_named(embed, "Closing note").value == "sorted in DMs"


def test_claiming_then_closing_stays_within_limits():
    embed = build()
    embed = embeds.mark_claimed(embed, FakeUser())
    embed = embeds.mark_closed(embed, FakeUser(), "done")
    assert len(embed) <= embeds.MAX_TOTAL
    assert len(embed.fields) <= embeds.MAX_FIELDS
