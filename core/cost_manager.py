"""
core/cost_manager.py
Tracks all API spending, enforces per-API budgets,
logs every transaction, and surfaces cost estimates before execution.
"""

from datetime import datetime
from typing import Optional
from .project import Project


class BudgetExceededError(Exception):
    pass


class GateLockError(Exception):
    pass


class CostManager:

    def __init__(self, project: Project):
        self.project = project

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    def check_api_allowed(self, api_name: str, required_gate: Optional[str] = None):
        """
        Raises if:
        - API is disabled
        - Required gate is not approved
        - Budget would be exceeded (checked separately with estimate)
        """
        if not self.project.is_api_enabled(api_name):
            raise GateLockError(f"API '{api_name}' is disabled in project config.")

        if required_gate:
            if not self.project.is_gate_open(required_gate):
                raise GateLockError(
                    f"API '{api_name}' is locked until gate '{required_gate}' is approved.\n"
                    f"Run: orchestrator approve-gate {required_gate}"
                )

    def check_budget(self, api_name: str, estimated_cost: float):
        remaining = self.project.get_budget_remaining(api_name)
        if estimated_cost > remaining:
            raise BudgetExceededError(
                f"Budget exceeded for '{api_name}'. "
                f"Estimated: ${estimated_cost:.4f}, Remaining: ${remaining:.4f}. "
                f"Increase budget in project.json or reduce scope."
            )

    def estimate_elevenlabs(self, text: str) -> float:
        """Rough estimate: ~$0.30 per 1000 characters."""
        return round((len(text) / 1000) * 0.30, 4)

    def estimate_meshy(self, detail_level: str) -> float:
        """Approximate Meshy costs by detail level."""
        rates = {"hero": 0.50, "medium": 0.25, "low": 0.10}
        return rates.get(detail_level, 0.25)

    def estimate_claude(self, input_tokens: int, output_tokens: int) -> float:
        """Claude Sonnet approximate pricing."""
        input_cost = (input_tokens / 1_000_000) * 3.00
        output_cost = (output_tokens / 1_000_000) * 15.00
        return round(input_cost + output_cost, 6)

    def estimate_imagen(self, image_count: int = 1) -> float:
        """Google Imagen 4.0 ~$0.04 per image."""
        return round(image_count * 0.04, 4)

    def estimate_stability(self, image_count: int = 1) -> float:
        """Stability SD3 Large Turbo ~4 credits = ~$0.04 per image."""
        return round(image_count * 0.04, 4)

    def estimate_comfyui(self, image_count: int = 1) -> float:
        """ComfyUI local generation — always free."""
        return 0.0

    # ------------------------------------------------------------------
    # Transaction recording
    # ------------------------------------------------------------------

    def record(
        self,
        api_name: str,
        cost_usd: float,
        stage: str,
        description: str,
        entity_id: Optional[str] = None
    ):
        """
        Records a completed API transaction to the ledger.
        Updates totals in project.json and ledger.json.
        """
        ledger = self.project.load_cost_ledger()
        ledger.setdefault("by_api", {})
        ledger.setdefault("by_stage", {})
        ledger.setdefault("by_chapter", {})
        ledger.setdefault("transactions", [])
        ledger.setdefault("totals", {"total": 0.0})

        # Build transaction record
        tx = {
            "timestamp": datetime.now().isoformat(),
            "api": api_name,
            "cost_usd": cost_usd,
            "stage": stage,
            "description": description,
            "entity_id": entity_id
        }
        ledger["transactions"].append(tx)

        # Update by_api totals
        if api_name not in ledger["by_api"]:
            ledger["by_api"][api_name] = {"spent": 0.00, "budget": 0.00}
        ledger["by_api"][api_name]["spent"] = round(
            ledger["by_api"][api_name]["spent"] + cost_usd, 6
        )

        # Update per-api total in totals dict
        ledger["totals"][api_name] = round(
            ledger["totals"].get(api_name, 0.0) + cost_usd, 6
        )

        # Update by_stage totals
        if stage not in ledger["by_stage"]:
            ledger["by_stage"][stage] = {"spent": 0.00}
        ledger["by_stage"][stage]["spent"] = round(
            ledger["by_stage"][stage]["spent"] + cost_usd, 6
        )

        # Update grand total
        ledger["totals"]["total"] = round(
            ledger["totals"].get("total", 0.0) + cost_usd, 6
        )

        ledger["last_updated"] = datetime.now().isoformat()

        self.project.save_cost_ledger(ledger)

        # Also update chapter cost if entity_id contains a chapter reference
        if entity_id and entity_id.startswith("ch"):
            chapter_id = entity_id.split("_")[0] if "_" in entity_id else entity_id
            try:
                chapter = self.project.load_chapter(chapter_id)
                if stage in chapter["costs"]["stage_totals_usd"]:
                    chapter["costs"]["stage_totals_usd"][stage] = round(
                        chapter["costs"]["stage_totals_usd"][stage] + cost_usd, 6
                    )
                chapter["costs"]["chapter_total_usd"] = round(
                    chapter["costs"]["chapter_total_usd"] + cost_usd, 6
                )
                self.project.save_chapter(chapter_id, chapter)
            except FileNotFoundError:
                pass

        print(f"  $ Recorded ${cost_usd:.4f} [{api_name}] -- {description}")

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def print_summary(self):
        ledger = self.project.load_cost_ledger()
        totals = ledger.get("totals", {})
        total = totals.get("total", 0.0)
        # Sum all API budgets for total budget
        budget = sum(
            (self.project.get_api_config(api).get("budget_limit_usd")
             or self.project.get_api_config(api).get("budget_usd", 0))
            for api in ["claude", "elevenlabs", "meshy", "cartwheel", "google_imagen", "stabilityai", "comfyui"]
        )
        pct = (total / budget * 100) if budget > 0 else 0

        print("\n+------------------------------------------+")
        print(f"|  Cost Summary — {self.project.id:<24} |")
        print("+------------------------------------------+")
        print(f"|  Total Spent:  ${total:>8.2f} / ${budget:.2f}  ({pct:.1f}%)  |")
        print("+------------------+----------+-----------+")
        print("|  API             |  Spent   |  Remaining|")
        print("+------------------+----------+-----------+")

        for api, data in ledger["by_api"].items():
            spent = data["spent"]
            api_cfg = self.project.get_api_config(api)
            budget_api = api_cfg.get("budget_limit_usd") or api_cfg.get("budget_usd", 0)
            remaining = round(budget_api - spent, 2)
            print(f"|  {api:<16}| ${spent:>7.2f} | ${remaining:>8.2f}|")

        print("+------------------+----------+-----------+")

    def print_stage_summary(self):
        ledger = self.project.load_cost_ledger()
        print("\n  Cost by Stage:")
        for stage, data in ledger["by_stage"].items():
            if data["spent"] > 0:
                print(f"    {stage:<20} ${data['spent']:.4f}")
