"""Command-line interface for EmbedForge."""

import argparse
import sys
from rich.table import Table
from rich.panel import Panel
from rich.tree import Tree
from embedforge.core.logger import console
from embedforge.pipeline import EmbedForgePipeline

def print_banner():
    console.print(Panel.fit(
        "[bold cyan]EmbedForge: Autonomous AI-Assisted Embedded Engineering System[/bold cyan]\n"
        "[italic magenta]Powered by NVIDIA Nemotron-3-Nano-30B-A3B & Nebius Token Factory[/italic magenta]\n"
        "[yellow]Deterministic Verification | Tavily Fact Discovery | Human-Gated Truth Store[/yellow]",
        border_style="cyan"
    ))

def display_results(results: dict):
    kb = results["approved_knowledge"]
    report = results["verification_report"]
    repair = results.get("repair_session")
    lineage = results["lineage_tree"]

    # 1. Knowledge Base Table
    kb_table = Table(title="[bold green]Approved Knowledge Base (Provenance-Linked Facts)[/bold green]")
    kb_table.add_column("Fact ID", style="cyan")
    kb_table.add_column("Subject", style="magenta")
    kb_table.add_column("Property", style="yellow")
    kb_table.add_column("Verified Value", style="bold green")
    kb_table.add_column("Source Type", style="blue")
    kb_table.add_column("Trust Level", style="green")

    for item in kb.items:
        kb_table.add_row(
            item.id,
            item.subject,
            item.property_name,
            str(item.value),
            item.source_type,
            item.trust_level.value
        )
    console.print(kb_table)
    console.print()

    # 2. Lineage Tree
    tree = Tree("[bold cyan]Core Artifact Lineage Traceability[/bold cyan]")
    for art_id, node in lineage.items():
        tree.add(f"[bold yellow]{art_id}[/bold yellow] ({node.artifact_type}) -> [white]{node.summary}[/white]")
    console.print(tree)
    console.print()

    # 3. Repair / Reverification Summary if executed
    if repair:
        console.print(Panel(
            f"[bold green]Autonomous Repair Loop Success![/bold green]\n"
            f"Evidence Case: [cyan]{repair.case_id}[/cyan]\n"
            f"Patch Plan: [yellow]{repair.patch_plan.id}[/yellow]\n"
            f"Rebuild ID: [blue]{repair.rebuild_id}[/blue] | Reverification ID: [green]{repair.reverify_id}[/green]\n"
            f"Status: [bold green]VERIFIED_PASS[/bold green]",
            border_style="green",
            title="Repair & Reverification Summary"
        ))

def main():
    parser = argparse.ArgumentParser(description="EmbedForge CLI - Autonomous Embedded Engineering System")
    parser.add_argument(
        "--prompt",
        type=str,
        default="Create an STM32 driver for MPU6050 accelerometer over I2C at 100Hz with 30ms startup delay.",
        help="Natural language embedded system requirement."
    )
    parser.add_argument(
        "--inject-defect",
        type=str,
        choices=["NONE", "WRONG_I2C_ADDRESS", "WRONG_WHO_AM_I_REG"],
        default="WRONG_I2C_ADDRESS",
        help="Inject a known silicon/firmware defect to demonstrate autonomous verification and repair."
    )
    args = parser.parse_args()

    print_banner()

    defect = None if args.inject_defect == "NONE" else args.inject_defect
    pipeline = EmbedForgePipeline()
    results = pipeline.run(user_prompt=args.prompt, inject_initial_defect=defect)

    display_results(results)

if __name__ == "__main__":
    main()
