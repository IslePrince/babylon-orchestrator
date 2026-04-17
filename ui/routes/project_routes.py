"""
ui/routes/project_routes.py — HTML page routes.

Every page renders a Jinja2 template and passes the project slug
plus global context (project list for the switcher).
"""

from flask import Blueprint, render_template, redirect, url_for, current_app

project_bp = Blueprint("project", __name__)


def _template_context(slug: str) -> dict:
    """Common context passed to every page template."""
    from ui.server import get_project
    projects = []
    for pid, path in current_app.config["PROJECTS"].items():
        try:
            from core.project import Project
            p = Project(path)
            projects.append({
                "slug": pid,
                "display_name": p.data.get("display_name", pid),
            })
        except Exception:
            projects.append({"slug": pid, "display_name": pid})
    return {"slug": slug, "projects": projects}


# ------------------------------------------------------------------
# Index — redirect to first project dashboard
# ------------------------------------------------------------------

@project_bp.route("/")
def index():
    projects = current_app.config["PROJECTS"]
    if projects:
        first_slug = next(iter(projects))
        return redirect(url_for("project.dashboard", slug=first_slug))
    return redirect(url_for("project.wizard"))


# ------------------------------------------------------------------
# Page routes — one per nav item
# ------------------------------------------------------------------

@project_bp.route("/project/<slug>/dashboard")
def dashboard(slug):
    ctx = _template_context(slug)
    return render_template("dashboard.html", **ctx)


@project_bp.route("/project/<slug>/ingest")
def ingest(slug):
    ctx = _template_context(slug)
    return render_template("ingest.html", **ctx)


@project_bp.route("/project/<slug>/characters")
def characters(slug):
    ctx = _template_context(slug)
    return render_template("characters.html", **ctx)


@project_bp.route("/project/<slug>/screenplay")
def screenplay(slug):
    ctx = _template_context(slug)
    return render_template("screenplay.html", **ctx)


@project_bp.route("/project/<slug>/storyboard")
def storyboard(slug):
    ctx = _template_context(slug)
    return render_template("storyboard.html", **ctx)


@project_bp.route("/project/<slug>/voices")
def voices(slug):
    ctx = _template_context(slug)
    return render_template("voices.html", **ctx)


@project_bp.route("/project/<slug>/screenplay-review")
def screenplay_review(slug):
    ctx = _template_context(slug)
    return render_template("screenplay_review.html", **ctx)


@project_bp.route("/project/<slug>/editing-room")
def editing_room(slug):
    ctx = _template_context(slug)
    return render_template("editing_room.html", **ctx)


@project_bp.route("/project/<slug>/assets")
def assets(slug):
    ctx = _template_context(slug)
    return render_template("assets.html", **ctx)


@project_bp.route("/project/<slug>/costs")
def costs(slug):
    ctx = _template_context(slug)
    return render_template("costs.html", **ctx)


# ------------------------------------------------------------------
# New project wizard
# ------------------------------------------------------------------

@project_bp.route("/new")
def wizard():
    projects = []
    for pid, path in current_app.config["PROJECTS"].items():
        projects.append({"slug": pid, "display_name": pid})
    return render_template("wizard.html", slug=None, projects=projects)
