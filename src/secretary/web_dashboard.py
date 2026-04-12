"""
Web dashboard for Claude Secretary monitoring.
Lightweight Flask + HTMX application for real-time visibility into autonomous operations.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS

from .dashboard_data import DashboardData

logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, template_folder=Path(__file__).parent.parent.parent / "templates")
CORS(app)

# Initialize dashboard data layer
dashboard = DashboardData()


@app.route("/")
def index():
    """Render main dashboard page."""
    return render_template("dashboard.html")


@app.route("/api/heartbeat")
def api_heartbeat():
    """Return live uptime, status, and pass rate."""
    try:
        data = dashboard.get_heartbeat()
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error fetching heartbeat: {e}")
        return jsonify({"error": str(e), "status": "unknown"}), 500


@app.route("/api/metrics")
def api_metrics():
    """Return cumulative task metrics and cost tracking."""
    try:
        data = dashboard.get_metrics()
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error fetching metrics: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/recent-tasks")
def api_recent_tasks():
    """Return recent tasks with optional filtering."""
    try:
        status_filter = request.args.get("status", None)
        limit = int(request.args.get("limit", 20))
        data = dashboard.get_recent_tasks(limit=limit, status_filter=status_filter)
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error fetching recent tasks: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/task/<task_id>")
def api_task_detail(task_id):
    """Return full details of a specific task."""
    try:
        data = dashboard.get_task_detail(task_id)
        if not data:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error fetching task detail: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


def run(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    """
    Start the Flask development server.
    
    Args:
        host: Host to bind to
        port: Port to listen on
        debug: Enable debug mode
    """
    logger.info(f"Starting dashboard on {host}:{port}")
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(debug=True)
