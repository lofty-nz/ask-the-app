"""Ask the App: ask a program a question (typed or spoken) and get an answer found in its help and code.

Use it from Python without the web server:

    from askapp import config, answer
    cfg = config.load()                       # or config.load(extra_projects={"me": {"root": "/path"}})
    print(answer.ask(cfg, "me", "how do I add a job?")["answer"])
"""
__version__ = "0.1.0"
