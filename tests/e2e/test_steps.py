"""The Steps popover lists exactly what was applied to the loaded data, and copies it as a script."""
from conftest import fixture_path


def steps(app):
    return app.js("[...document.querySelectorAll('#steps-list li')].map(li => [li.className, li.querySelector('pre').textContent])")


def test_steps_list_tracks_runs_and_reset(app):
    app.load(fixture_path("sample.csv"))
    assert app.js("document.getElementById('steps-count').hidden") and steps(app) == []
    app.run("df.shape")                    # looking only: not listed
    app.run("k = 2")                       # defines a name: listed, Reset enabled
    assert steps(app) == [["define", "k = 2"]]
    assert not app.js("document.getElementById('reset').disabled")
    app.run("df['v2'] = df.value * k")
    app.run("df[df.v2 > 20]")
    assert steps(app) == [["define", "k = 2"], ["update", "df['v2'] = df.value * k"], ["view", "df[df.v2 > 20]"]]
    assert app.text("#steps-count") == "3"
    app.page.click("#steps summary")
    assert app.visible("#steps-list") and "sample.csv" in app.text("#steps-loaded")
    assert "changed df · 500 × 8" in app.text("#steps-list")
    app.page.click("#expr")                # outside click closes it
    assert not app.js("document.getElementById('steps').open")
    app.page.click("#reset")
    app.idle()
    assert steps(app) == [] and app.js("document.getElementById('steps-count').hidden")
    assert app.js("document.getElementById('reset').disabled")


def test_copy_as_python_gives_a_replayable_script(app):
    app.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    app.load(fixture_path("sample.csv"))
    app.run("df['v2'] = df.value * 2")
    app.run("df.describe()")
    app.page.click("#steps summary")
    app.page.click("#steps-copy")
    app.wait("document.getElementById('steps-copy').textContent.includes('Copied')", 5, "copied")
    script = app.page.evaluate("navigator.clipboard.readText()")
    assert 'df = pd.read_csv("sample.csv")' in script
    assert "# 1. changed df\ndf['v2'] = df.value * 2\n\n# 2. result became the view\ndf = df.describe()\n" in script


def test_copy_falls_back_without_the_clipboard_api(app):
    # plain http on a LAN (e.g. testing from a phone) has no navigator.clipboard
    app.load(fixture_path("sample.csv"))
    app.run("df['v2'] = 1")
    app.js("Object.defineProperty(navigator, 'clipboard', { value: undefined })")
    app.page.click("#steps summary")
    app.page.click("#steps-copy")
    app.wait("document.getElementById('steps-copy').textContent.includes('Copied')", 5, "copied via execCommand")
    assert not app.js("document.querySelector('textarea.offscreen')")
