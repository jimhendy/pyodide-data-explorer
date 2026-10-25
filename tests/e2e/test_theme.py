"""Theme button: system -> light -> dark, remembered, applied before paint, and the plot recoloured in place."""
from conftest import fixture_path
from test_plot_and_changes import draw

BG = "getComputedStyle(document.body).backgroundColor"
LIGHT_BG, DARK_BG = "rgb(244, 245, 247)", "rgb(13, 15, 18)"


def test_theme_button_cycles_and_is_remembered(make_app):
    app = make_app()
    app.page.emulate_media(color_scheme="light")
    assert app.js("document.documentElement.dataset.theme") is None
    assert app.js("document.getElementById('theme-btn').title") == "Theme: match system"
    app.page.click("#theme-btn")
    assert app.js("document.documentElement.dataset.theme") == "light"
    app.page.click("#theme-btn")
    assert app.js("document.documentElement.dataset.theme") == "dark" and app.js(BG) == DARK_BG
    assert app.js("document.getElementById('theme-btn').title") == "Theme: dark"
    # remembered across visits, and applied by theme.js before app.js runs
    app.open()
    assert app.js("document.documentElement.dataset.theme") == "dark" and app.js(BG) == DARK_BG
    app.page.click("#theme-btn")  # back to system (light here)
    assert app.js("document.documentElement.dataset.theme") is None and app.js(BG) == LIGHT_BG
    assert app.js("localStorage.getItem('data-explorer:theme')") is None


def test_explicit_light_overrides_a_dark_system(make_app):
    app = make_app()
    app.page.emulate_media(color_scheme="dark")
    assert app.js(BG) == DARK_BG
    app.page.click("#theme-btn")
    assert app.js(BG) == LIGHT_BG


def test_plot_is_recoloured_without_losing_the_zoom(app):
    app.page.emulate_media(color_scheme="light")
    app.load(fixture_path("sample.csv"))
    draw(app, "scatter", 3, [4])
    app.js("Plotly.relayout('plot', {'xaxis.range': [1, 2]})")
    light = app.js("document.getElementById('plot').layout.font.color")
    app.page.click("#theme-btn")
    app.page.click("#theme-btn")  # dark
    app.wait("document.getElementById('plot').layout.font.color !== %r" % light, 10, "plot recoloured")
    assert app.js("document.getElementById('plot').layout.font.color") == "#e7e9ed"
    assert app.js("document.getElementById('plot').layout.xaxis.range") == [1, 2]
