// Dashboard JavaScript Logic

class Dashboard {
    constructor() {
        this.refreshInterval = 5000; // 5 seconds
        this.currentFilter = "";
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.loadData();
        this.startAutoRefresh();
    }

    setupEventListeners() {
        // Refresh button
        const refreshBtn = document.getElementById("refresh-btn");
        if (refreshBtn) {
            refreshBtn.addEventListener("click", () => this.loadData());
        }

        // Filter buttons
        document.querySelectorAll(".filter-controls .btn").forEach(btn => {
            btn.addEventListener("click", (e) => {
                document.querySelectorAll(".filter-controls .btn").forEach(b => b.classList.remove("active"));
                e.target.classList.add("active");
                this.currentFilter = e.target.dataset.filter;
                this.loadRecentTasks();
            });
        });
    }

    startAutoRefresh() {
        setInterval(() => this.loadData(), this.refreshInterval);
    }

    async loadData() {
        await Promise.all([
            this.loadHeartbeat(),
            this.loadMetrics(),
            this.loadRecentTasks()
        ]);
        this.updateLastUpdate();
    }

    async loadHeartbeat() {
        try {
            const response = await fetch("/api/heartbeat");
            const data = await response.json();

            document.getElementById("uptime-display").textContent = data.uptime_human || "--";
            document.getElementById("pass-rate-display").textContent = data.pass_rate_percent || "--";

            const badge = document.getElementById("status-badge");
            badge.textContent = data.status.toUpperCase();
            badge.className = `status-badge status-${data.status}`;
        } catch (error) {
            console.error("Error loading heartbeat:", error);
        }
    }

    async loadMetrics() {
        try {
            const response = await fetch("/api/metrics");
            const data = await response.json();

            document.getElementById("task-count-display").textContent = data.total || 0;
            document.getElementById("pass-fail-display").textContent = `${data.passed || 0} / ${data.failed || 0}`;
            document.getElementById("cost-display").textContent = `$${data.cost_usd || "0.00"}`;
        } catch (error) {
            console.error("Error loading metrics:", error);
        }
    }

    async loadRecentTasks() {
        try {
            const url = `/api/recent-tasks?limit=20${this.currentFilter ? `&status=${this.currentFilter}` : ""}`;
            const response = await fetch(url);
            const tasks = await response.json();

            const container = document.getElementById("tasks-container");
            if (tasks.length === 0) {
                container.innerHTML = "<p class='loading'>No tasks found.</p>";
                return;
            }

            container.innerHTML = tasks.map(task => `
                <div class="task-item ${task.status}" data-task-id="${task.id}">
                    <div class="task-info">
                        <div class="task-title">${this.escapeHtml(task.task)}</div>
                        <div class="task-meta">
                            <span>${task.tier}</span> • 
                            <span>${task.duration}</span>
                            ${task.error ? `• <span class="error">${this.escapeHtml(task.error.substring(0, 50))}</span>` : ""}
                        </div>
                    </div>
                    <div class="task-status ${task.status}">${task.status.toUpperCase()}</div>
                </div>
            `).join("");

            // Add click handlers for task details
            container.querySelectorAll(".task-item").forEach(item => {
                item.addEventListener("click", () => this.showTaskDetail(item.dataset.taskId));
            });
        } catch (error) {
            console.error("Error loading recent tasks:", error);
        }
    }

    async showTaskDetail(taskId) {
        try {
            const response = await fetch(`/api/task/${taskId}`);
            const task = await response.json();
            
            // Simple modal or expand-in-place
            console.log("Task Detail:", task);
            alert(`Task: ${task.task}\nStatus: ${task.status}\nDuration: ${task.duration_seconds}s`);
        } catch (error) {
            console.error("Error loading task detail:", error);
        }
    }

    updateLastUpdate() {
        const now = new Date();
        document.getElementById("last-update").textContent = `Last updated: ${now.toLocaleTimeString()}`;
    }

    escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize dashboard when DOM is ready
document.addEventListener("DOMContentLoaded", () => {
    window.dashboard = new Dashboard();
});
