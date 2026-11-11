FALLBACK: dict[int, str] = {
    0: ("Hi {first_name}, this is {agent_name}. Great meeting you at {where}! "
        "Happy to share more whenever it suits you, no rush at all."),
    1: ("Hi {first_name}, {agent_name} here. Just a quick hello after we met at {where}. "
        "Hope your week is going well!"),
    2: ("Hi {first_name}, it's {agent_name} again. Hope all is good with you! "
        "Let me know if you'd like to catch up over a coffee, no pressure at all."),
    3: ("Hi {first_name}, {agent_name} here. This is my last note for now, so I won't message again. "
        "If you ever want to chat, you're welcome to reach out anytime. Take care!"),
}
