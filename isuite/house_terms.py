"""Words a draft must never contain because of your own company.

These are added to the general banned list in drafts.py. A draft that contains any of them is
replaced by safe fallback wording.

To add your company's name, edit the two lines below. For example:

    EXTRA_BANNED = ("Acme Insure",)       # matched anywhere, ignoring capitals
    EXTRA_BANNED_WORDS = ("ACME",)        # matched as a whole word, with the same capitals

Leave them empty to use only the general list.
"""
EXTRA_BANNED = ()
EXTRA_BANNED_WORDS = ()

# Shown in the "ask for an invite" reply of a team set-up. Leave empty for a generic sentence.
AGENCY_NAME = ""
