"""
ui/server.py — Babylon Studio Web UI

Flask application factory. Scans a projects directory for project.json files,
registers blueprints, and serves the dashboard on port 5757.

Usage:
    python ui/server.py --projects-dir ~/studio/projects
"""

import argparse
import os
import sys
from pathlib import Path

# Add orchestrator root to sys.path so core/ and stages/ imports resolve
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, abort

from core.project import Project
from ui.version import get_current_version


def scan_projects(projects_dir: str) -> dict:
    """Scan directory for subdirectories containing project.json.
    Returns {slug: absolute_path} dict.
    """
    projects = {}
    pd = Path(projects_dir)
    if not pd.is_dir():
        return projects

    # Directories to skip (templates, internals)
    skip = {"project_template", "__pycache__", ".git", "node_modules"}

    for entry in sorted(pd.iterdir()):
        if entry.name in skip:
            continue
        if entry.is_dir() and (entry / "project.json").exists():
            try:
                p = Project(str(entry))
                # Skip unresolved template placeholders
                if "{{" in p.id:
                    continue
                projects[p.id] = str(entry)
            except Exception:
                pass
    return projects


def get_project(slug: str) -> Project:
    """Instantiate a fresh Project for this request. No caching."""
    from flask import current_app
    path = current_app.config["PROJECTS"].get(slug)
    if not path:
        abort(404, description=f"Project '{slug}' not found")
    return Project(path)


def create_app(projects_dir: str = None) -> Flask:
    projects_dir = projects_dir or os.getenv("DEFAULT_PROJECTS_DIR", ".")
    projects_dir = str(Path(projects_dir).resolve())

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["PROJECTS_DIR"] = projects_dir
    app.config["PROJECTS"] = scan_projects(projects_dir)
    app.config["SECRET_KEY"] = os.urandom(24)

    # Make current version available to all templates
    @app.context_processor
    def inject_version():
        return {"app_version": get_current_version()}

    # Register blueprints
    from ui.routes.project_routes import project_bp
    from ui.routes.api_routes import api_bp
    from ui.routes.stage_routes import stage_bp

    app.register_blueprint(project_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(stage_bp, url_prefix="/api")

    # JSON error handlers for API routes
    @app.errorhandler(404)
    def not_found(e):
        from flask import request, jsonify, render_template
        if request.path.startswith("/api/"):
            return jsonify({"error": str(e.description)}), 404
        return render_template("error.html", code=404, message=str(e.description)), 404

    @app.errorhandler(400)
    def bad_request(e):
        from flask import request, jsonify, render_template
        if request.path.startswith("/api/"):
            return jsonify({"error": str(e.description)}), 400
        return render_template("error.html", code=400, message=str(e.description)), 400

    @app.errorhandler(500)
    def server_error(e):
        from flask import request, jsonify, render_template
        if request.path.startswith("/api/"):
            return jsonify({"error": "Internal server error"}), 500
        return render_template("error.html", code=500, message="Internal server error"), 500

    project_count = len(app.config["PROJECTS"])
    print(f"\n  Babylon Studio v{get_current_version()}")
    print(f"  Projects directory: {projects_dir}")
    print(f"  Found {project_count} project{'s' if project_count != 1 else ''}")
    print(f"  Running at: http://localhost:5757\n")

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Babylon Studio Web UI")
    parser.add_argument(
        "--projects-dir",
        default=os.getenv("DEFAULT_PROJECTS_DIR", "."),
        help="Directory containing project folders",
    )
    parser.add_argument("--port", type=int, default=5757)
    parser.add_argument("--debug", action="store_true", default=True)
    args = parser.parse_args()

    app = create_app(projects_dir=args.projects_dir)
    # use_reloader=False is critical: the reloader restarts the worker
    # process when files change, which kills all SSE connections and
    # wipes in-memory job state (job_queues, job_meta).
    # Stage runs write JSON/MD files that can trigger reloads.
    app.run(host="0.0.0.0", port=args.port, debug=args.debug,
            use_reloader=False, threaded=True)
