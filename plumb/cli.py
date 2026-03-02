from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from plumb.config import (
    PlumbConfig,
    find_repo_root,
    ensure_plumb_dir,
    load_config,
    save_config,
)
from plumb.ignore import DEFAULT_PLUMBIGNORE
from plumb.decision_log import (
    Decision,
    read_all_decisions,
    append_decision,
    update_decision_status,
    filter_decisions,
    find_decision_branch,
)

console = Console()


@click.group()
def cli():
    """Plumb: Keep spec, tests, and code in sync."""
    pass


@cli.command()
def init():
    """Initialize Plumb in the current git repository."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    # Create .plumb/
    ensure_plumb_dir(repo_root)

    # Prompt for spec paths
    spec_input = click.prompt(
        "Path to spec file or directory of spec markdown files",
        default=".",
    )
    spec_path = repo_root / spec_input
    if not spec_path.exists():
        console.print(f"[red]Error: Path '{spec_input}' does not exist.[/red]")
        raise SystemExit(1)
    # Validate .md files exist
    if spec_path.is_dir():
        md_files = list(spec_path.rglob("*.md"))
        if not md_files:
            console.print(
                f"[red]Error: No .md files found in '{spec_input}'.[/red]"
            )
            raise SystemExit(1)

    # Prompt for test paths
    test_input = click.prompt(
        "Path to test file or test directory", default="tests/"
    )
    test_path = repo_root / test_input
    if not test_path.exists():
        console.print(f"[yellow]Warning: Path '{test_input}' does not exist. Creating it.[/yellow]")
        test_path.mkdir(parents=True, exist_ok=True)

    # Save config
    cfg = PlumbConfig(
        spec_paths=[spec_input],
        test_paths=[test_input],
        initialized_at=datetime.now(timezone.utc).isoformat(),
    )
    save_config(repo_root, cfg)

    # Install git hooks
    hooks_dir = repo_root / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text("#!/bin/sh\nplumb hook\nexit $?\n")
    hook_path.chmod(0o755)
    post_commit_path = hooks_dir / "post-commit"
    post_commit_path.write_text("#!/bin/sh\nplumb post-commit\n")
    post_commit_path.chmod(0o755)

    # Create default .plumbignore if it doesn't exist
    plumbignore_path = repo_root / ".plumbignore"
    if not plumbignore_path.exists():
        plumbignore_path.write_text(DEFAULT_PLUMBIGNORE)

    # Install skill file
    skill_dir = repo_root / ".claude" / "skills" / "plumb"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_src = Path(__file__).parent / "skill" / "SKILL.md"
    skill_dst = skill_dir / "SKILL.md"
    if skill_src.exists():
        shutil.copy2(str(skill_src), str(skill_dst))
    else:
        console.print("[yellow]Warning: SKILL.md source not found in package.[/yellow]")

    # CLAUDE.md integration
    _update_claude_md(repo_root, cfg)

    # Run parse-spec
    try:
        from plumb.sync import parse_spec_files
        parse_spec_files(repo_root)
        console.print("Parsed spec into requirements.")
    except Exception as e:
        console.print(f"[yellow]Warning: Could not parse spec: {e}[/yellow]")

    console.print(f"\n[green]Plumb initialized successfully![/green]")
    console.print(f"  Config: .plumb/config.json")
    console.print(f"  Hooks: .git/hooks/pre-commit, post-commit")
    console.print(f"  Ignore: .plumbignore")
    console.print(f"  Skill: .claude/skills/plumb/SKILL.md")
    console.print(f"  Spec: {spec_input}")
    console.print(f"  Tests: {test_input}")


def _coverage_bar(covered: int, total: int, width: int = 20) -> str:
    """Render an inline coverage bar like: ████████░░░░░░░░░░░░ 37%  (60/163)"""
    pct = (covered / total) * 100 if total else 0
    filled = round(width * covered / total) if total else 0
    bar = "█" * filled + "░" * (width - filled)
    color = "green" if pct >= 70 else "yellow" if pct >= 40 else "red"
    return f"[{color}]{bar}[/{color}] {pct:.0f}%  ({covered}/{total})"


def _update_claude_md(repo_root: Path, cfg: PlumbConfig) -> None:
    """Append/update Plumb block in CLAUDE.md."""
    claude_md = repo_root / "CLAUDE.md"
    spec_list = ", ".join(cfg.spec_paths)
    test_list = ", ".join(cfg.test_paths)

    block = f"""<!-- plumb:start -->
## Plumb (Spec/Test/Code Sync)

This project uses Plumb to keep the spec, tests, and code in sync.

- **Spec:** {spec_list}
- **Tests:** {test_list}
- **Decision log:** `.plumb/decisions.jsonl`

### When working in this project:

- Run `plumb status` before beginning work to understand current alignment.
- Run `plumb diff` before committing to preview what Plumb will capture.
- When `git commit` is intercepted by Plumb, **use `AskUserQuestion`** to present
  each pending decision via the native multiple-choice UI. Options: Approve,
  Ignore, Reject. Then run the corresponding `plumb` command.
  **NEVER approve, reject, or edit decisions on the user's behalf.** This is
  non-negotiable.
- After all decisions are resolved, run `plumb sync` to update the spec and
  generate tests. Stage the sync output, then re-run `git commit`. Draft the
  commit message **after** decision review and include a list of approved
  decisions.
- Use `plumb coverage` to identify what needs to be implemented or tested next.
- Never edit `.plumb/decisions.jsonl` directly.
- Treat the spec markdown files as the source of truth for intended behavior.
  Plumb will keep them updated as decisions are approved.
<!-- plumb:end -->"""

    if claude_md.exists():
        content = claude_md.read_text()
        # Check for existing markers
        import re
        pattern = r"<!-- plumb:start -->.*?<!-- plumb:end -->"
        if re.search(pattern, content, re.DOTALL):
            content = re.sub(pattern, block, content, flags=re.DOTALL)
        else:
            content = content.rstrip() + "\n\n" + block + "\n"
        claude_md.write_text(content)
    else:
        claude_md.write_text(block + "\n")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview only, don't write decisions")
def hook(dry_run):
    """Run the pre-commit hook analysis."""
    from plumb.git_hook import run_hook

    repo_root = find_repo_root()
    exit_code = run_hook(repo_root, dry_run=dry_run)
    raise SystemExit(exit_code)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview only, don't write decisions")
def check(dry_run):
    """Scan staged changes for decisions (alias for hook)."""
    from plumb.git_hook import run_hook

    repo_root = find_repo_root()
    exit_code = run_hook(repo_root, dry_run=dry_run)
    raise SystemExit(exit_code)


@cli.command(name="post-commit")
def post_commit():
    """Run the post-commit hook to update last_commit."""
    from plumb.git_hook import run_post_commit

    repo_root = find_repo_root()
    run_post_commit(repo_root)


@cli.command()
def diff():
    """Preview what Plumb will capture from staged changes (read-only)."""
    from plumb.git_hook import run_hook

    repo_root = find_repo_root()
    run_hook(repo_root, dry_run=True)


@cli.command()
@click.option("--branch", default=None, help="Filter by branch")
def review(branch):
    """Interactive review of pending decisions."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    pending = filter_decisions(repo_root, status="pending")
    if branch:
        pending = [d for d in pending if d.branch == branch]

    if not pending:
        console.print("No pending decisions.")
        return

    console.print(f"\n[bold]Plumb Review: {len(pending)} pending decision(s)[/bold]\n")

    # Pre-compute branch for each decision so updates target the correct shard
    branch_for = {d.id: find_decision_branch(repo_root, d.id) for d in pending}

    approved_ids = []
    for i, d in enumerate(pending, 1):
        console.print(f"[bold]Decision {i} of {len(pending)}[/bold] [{d.id}]")
        if d.question:
            console.print(f"  [cyan]Question:[/cyan] {d.question}")
        if d.decision:
            console.print(f"  [cyan]Decision:[/cyan] {d.decision}")
        console.print(f"  Made by: {d.made_by or 'unknown'} | Confidence: {d.confidence or 'N/A'}")
        if d.branch:
            console.print(f"  Branch: {d.branch}")
        if d.ref_status == "broken":
            console.print("  [red]Warning: Git reference is broken[/red]")
        console.print()

        action = click.prompt(
            "  [a]pprove / [i]gnore / [r]eject / [e]dit",
            type=click.Choice(["a", "i", "r", "e"], case_sensitive=False),
        )

        now = datetime.now(timezone.utc).isoformat()
        if action == "a":
            update_decision_status(repo_root, d.id, branch=branch_for.get(d.id), status="approved", reviewed_at=now)
            approved_ids.append(d.id)
            console.print("  [green]Approved.[/green]\n")
        elif action == "i":
            update_decision_status(repo_root, d.id, branch=branch_for.get(d.id), status="ignored", reviewed_at=now)
            console.print("  [dim]Ignored.[/dim]\n")
        elif action == "r":
            reason = click.prompt("  Rejection reason", default="")
            update_decision_status(
                repo_root, d.id, branch=branch_for.get(d.id), status="rejected",
                rejection_reason=reason, reviewed_at=now,
            )
            console.print("  [red]Rejected.[/red]")
            _run_modify(repo_root, d.id)
            console.print()
        elif action == "e":
            new_text = click.prompt("  New decision text")
            update_decision_status(
                repo_root, d.id, branch=branch_for.get(d.id), status="edited",
                decision=new_text, reviewed_at=now,
            )
            approved_ids.append(d.id)
            console.print("  [yellow]Edited.[/yellow]\n")

    if approved_ids:
        console.print(f"\n{len(approved_ids)} decision(s) resolved. "
                      "Run [bold]plumb sync[/bold] to update spec and tests.")


def _run_modify(repo_root: Path, decision_id: str) -> None:
    """Run the modify command for a rejected decision."""
    from plumb.programs.code_modifier import CodeModifier
    from git import Repo

    decisions = read_all_decisions(repo_root)
    target = None
    for d in decisions:
        if d.id == decision_id:
            target = d
            break

    if not target or target.status != "rejected":
        console.print(f"  [red]Decision {decision_id} not found or not rejected.[/red]")
        return

    repo = Repo(repo_root)
    staged_diff = repo.git.diff("--cached")
    if not staged_diff:
        console.print("  [yellow]No staged changes to modify.[/yellow]")
        return

    # Read spec
    config = load_config(repo_root)
    spec_content = ""
    if config:
        for sp in config.spec_paths:
            spec_file = repo_root / sp
            if spec_file.is_file():
                spec_content += spec_file.read_text()

    decision_branch = find_decision_branch(repo_root, decision_id)

    try:
        modifier = CodeModifier()
        modifications = modifier.modify(
            staged_diff=staged_diff,
            decision=target.decision or "",
            rejection_reason=target.rejection_reason or "",
            spec_content=spec_content,
        )
    except Exception as e:
        console.print(f"  [red]Code modification failed: {e}[/red]")
        update_decision_status(repo_root, decision_id, branch=decision_branch, status="rejected_manual")
        return

    if not modifications:
        console.print("  [yellow]No modifications produced.[/yellow]")
        update_decision_status(repo_root, decision_id, branch=decision_branch, status="rejected_manual")
        return

    # Apply modifications
    for filepath, content in modifications.items():
        full_path = repo_root / filepath
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    # Run pytest
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    is_tty = sys.stdout.isatty()

    if test_result.returncode == 0:
        # Stage modified files
        for filepath in modifications:
            repo.index.add([filepath])
        update_decision_status(repo_root, decision_id, branch=decision_branch, status="rejected_modified")
        if is_tty:
            console.print("  [green]Tests passed. Modified files staged.[/green]")
        else:
            diff_output = repo.git.diff("--cached")
            print(json.dumps({
                "id": decision_id,
                "result": "modified",
                "tests_passed": True,
                "diff": diff_output,
            }))
    else:
        update_decision_status(repo_root, decision_id, branch=decision_branch, status="rejected_manual")
        if is_tty:
            console.print("  [red]Tests failed. Modification not staged.[/red]")
            console.print(f"  {test_result.stdout}")
        else:
            print(json.dumps({
                "id": decision_id,
                "result": "failed",
                "tests_passed": False,
                "diff": "",
            }))


@cli.command()
@click.argument("decision_id", required=False, default=None)
@click.option("--all", "-a", "approve_all", is_flag=True, help="Approve all pending decisions")
def approve(decision_id, approve_all):
    """Approve a decision by ID, or all pending decisions with --all."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    if approve_all and decision_id:
        console.print("[red]Error: Cannot use --all with a specific decision ID.[/red]")
        raise SystemExit(1)

    if not approve_all and not decision_id:
        console.print("[red]Error: Provide a decision ID or use --all.[/red]")
        raise SystemExit(1)

    if approve_all:
        pending = filter_decisions(repo_root, status="pending")
        if not pending:
            console.print("[yellow]No pending decisions to approve.[/yellow]")
            return
        now = datetime.now(timezone.utc).isoformat()
        branch_for = {d.id: find_decision_branch(repo_root, d.id) for d in pending}
        for d in pending:
            update_decision_status(
                repo_root, d.id, branch=branch_for.get(d.id), status="approved", reviewed_at=now,
            )
        console.print(f"[green]Approved {len(pending)} decision(s).[/green]")
        console.print("Run [bold]plumb sync[/bold] to update spec and tests.")
        return

    now = datetime.now(timezone.utc).isoformat()
    branch = find_decision_branch(repo_root, decision_id)
    result = update_decision_status(
        repo_root, decision_id, branch=branch, status="approved", reviewed_at=now,
    )
    if result is None:
        console.print(f"[red]Decision '{decision_id}' not found.[/red]")
        raise SystemExit(1)

    console.print(f"[green]Approved {decision_id}.[/green]")
    console.print("Run [bold]plumb sync[/bold] to update spec and tests.")


@cli.command()
@click.argument("decision_id")
@click.option("--reason", default=None, help="Reason for rejection")
def reject(decision_id, reason):
    """Reject a decision by ID."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    now = datetime.now(timezone.utc).isoformat()
    branch = find_decision_branch(repo_root, decision_id)
    result = update_decision_status(
        repo_root, decision_id,
        branch=branch,
        status="rejected",
        rejection_reason=reason,
        reviewed_at=now,
    )
    if result is None:
        console.print(f"[red]Decision '{decision_id}' not found.[/red]")
        raise SystemExit(1)

    console.print(f"[yellow]Rejected {decision_id}.[/yellow]")
    _run_modify(repo_root, decision_id)


@cli.command()
@click.argument("decision_id")
def ignore(decision_id):
    """Ignore a decision by ID (mark as not spec-relevant)."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    now = datetime.now(timezone.utc).isoformat()
    branch = find_decision_branch(repo_root, decision_id)
    result = update_decision_status(
        repo_root, decision_id,
        branch=branch,
        status="ignored",
        reviewed_at=now,
    )
    if result is None:
        console.print(f"[red]Decision '{decision_id}' not found.[/red]")
        raise SystemExit(1)

    console.print(f"Ignored {decision_id}.")


@cli.command()
@click.argument("decision_id")
@click.argument("text")
def edit(decision_id, text):
    """Edit a decision's text and approve it."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    now = datetime.now(timezone.utc).isoformat()
    branch = find_decision_branch(repo_root, decision_id)
    result = update_decision_status(
        repo_root, decision_id,
        branch=branch,
        status="edited",
        decision=text,
        reviewed_at=now,
    )
    if result is None:
        console.print(f"[red]Decision '{decision_id}' not found.[/red]")
        raise SystemExit(1)

    console.print(f"[yellow]Edited {decision_id}.[/yellow]")
    console.print("Run [bold]plumb sync[/bold] to update spec and tests.")


@cli.command()
@click.argument("decision_id")
def modify(decision_id):
    """Modify staged code to satisfy a rejected decision."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    _run_modify(repo_root, decision_id)


@cli.command(name="sync")
def sync_cmd():
    """Sync all unsynced approved/edited decisions."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    from plumb.sync import sync_decisions
    try:
        with console.status("[bold cyan]Syncing decisions...", spinner="dots") as status:
            def on_progress(msg):
                status.update(f"[bold cyan]{msg}")
            result = sync_decisions(repo_root, on_progress=on_progress)
        console.print(f"Synced: {result['spec_updated']} spec sections updated, "
                      f"{result['tests_generated']} tests generated.")
    except Exception as e:
        console.print(f"[red]Sync failed: {e}[/red]")
        raise SystemExit(1)


@cli.command(name="parse-spec")
def parse_spec():
    """Parse spec files into requirements.json."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    from plumb.sync import parse_spec_files
    try:
        reqs = parse_spec_files(repo_root)
        console.print(f"Parsed {len(reqs)} requirements from spec files.")
    except Exception as e:
        console.print(f"[red]Parse failed: {e}[/red]")
        raise SystemExit(1)


@cli.command(name="map-tests")
@click.option("--dry-run", is_flag=True, help="Show proposed mappings without writing")
def map_tests(dry_run):
    """Map existing tests to requirements using LLM analysis."""
    import ast

    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    config = load_config(repo_root)
    if not config:
        console.print("[red]Error: Plumb not initialized.[/red]")
        raise SystemExit(1)

    req_path = Path(repo_root) / ".plumb" / "requirements.json"
    if not req_path.exists():
        console.print("[red]No requirements.json found. Run 'plumb parse-spec' first.[/red]")
        raise SystemExit(1)

    requirements = json.loads(req_path.read_text())
    if not requirements:
        console.print("[yellow]No requirements found.[/yellow]")
        return

    # Collect test summaries from all test files
    test_summaries = []
    test_files: dict[str, str] = {}  # rel_path -> content
    for tp in config.test_paths:
        test_dir = Path(repo_root) / tp
        if test_dir.is_file():
            files = [test_dir]
        elif test_dir.is_dir():
            files = list(test_dir.rglob("test_*.py"))
        else:
            continue
        for tf in files:
            content = tf.read_text()
            rel_path = str(tf.relative_to(repo_root))
            test_files[rel_path] = content
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            lines = content.split("\n")
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                    # Extract docstring
                    docstring = ast.get_docstring(node) or ""
                    # Extract first few lines of body for context
                    end_line = min(node.end_lineno or node.lineno + 10, len(lines))
                    body_preview = "\n".join(lines[node.lineno - 1:end_line])[:300]
                    test_summaries.append({
                        "file": rel_path,
                        "name": node.name,
                        "docstring": docstring,
                        "preview": body_preview,
                    })

    if not test_summaries:
        console.print("[yellow]No test functions found.[/yellow]")
        return

    console.print(f"Found {len(test_summaries)} test functions and {len(requirements)} requirements.")
    console.print("Running LLM mapping...")

    from plumb.programs import configure_dspy, run_with_retries
    from plumb.programs.test_mapper import TestMapper

    configure_dspy()
    mapper = TestMapper()

    req_json = json.dumps([{"id": r["id"], "text": r["text"]} for r in requirements])
    summaries_json = json.dumps(test_summaries)

    try:
        mappings = run_with_retries(mapper, req_json, summaries_json)
    except Exception as e:
        console.print(f"[red]Mapping failed: {e}[/red]")
        raise SystemExit(1)

    if not mappings:
        console.print("[yellow]No mappings found.[/yellow]")
        return

    # Display results
    table = Table(title="Proposed Test-to-Requirement Mappings")
    table.add_column("Test", style="cyan")
    table.add_column("File")
    table.add_column("Requirements", style="green")
    table.add_column("Confidence", justify="right")

    for m in mappings:
        table.add_row(
            m.test_function,
            m.file_path,
            ", ".join(m.requirement_ids),
            f"{m.confidence:.0%}",
        )
    console.print(table)

    if dry_run:
        console.print("\n[yellow]Dry run — no markers written.[/yellow]")
        return

    # Ask user for approval
    action = click.prompt(
        "\nApply these mappings? [y]es / [n]o / [s]elect individually",
        type=click.Choice(["y", "n", "s"], case_sensitive=False),
    )

    if action == "n":
        console.print("Aborted.")
        return

    approved_mappings = mappings
    if action == "s":
        approved_mappings = []
        for m in mappings:
            if click.confirm(
                f"  Map {m.test_function} -> {', '.join(m.requirement_ids)}?"
            ):
                approved_mappings.append(m)

    # Inject markers into test files
    injected = 0
    modified_files: dict[str, str] = {}
    for m in approved_mappings:
        file_path = m.file_path
        if file_path not in test_files:
            continue
        content = modified_files.get(file_path, test_files[file_path])
        lines = content.split("\n")
        # Find the function definition line
        for i, line in enumerate(lines):
            if f"def {m.test_function}(" in line:
                # Find indentation of the function body
                indent = "    "
                for j in range(i + 1, min(i + 5, len(lines))):
                    stripped = lines[j]
                    if stripped.strip():
                        indent = stripped[: len(stripped) - len(stripped.lstrip())]
                        break
                # Build marker comments
                markers = []
                for req_id in m.requirement_ids:
                    marker = f"{indent}# plumb:{req_id}"
                    # Check it's not already there
                    if marker not in content:
                        markers.append(marker)
                if markers:
                    marker_text = "\n".join(markers)
                    lines.insert(i + 1, marker_text)
                    injected += len(markers)
                break
        modified_files[file_path] = "\n".join(lines)

    # Write back modified files
    for rel_path, content in modified_files.items():
        (Path(repo_root) / rel_path).write_text(content)

    console.print(f"\n[green]Injected {injected} marker(s) across {len(modified_files)} file(s).[/green]")


@cli.command()
def coverage():
    """Run and print all three coverage dimensions."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    from plumb.coverage_reporter import print_coverage_report
    print_coverage_report(repo_root)


@cli.command()
def status():
    """Print a summary of the project's Plumb state."""
    repo_root = find_repo_root()
    if repo_root is None:
        console.print("[red]Error: Not a git repository.[/red]")
        raise SystemExit(1)

    config = load_config(repo_root)
    if config is None:
        console.print("[yellow]Plumb not initialized. Run 'plumb init'.[/yellow]")
        return
    
    # Spec files
    console.print(f"[cyan]Spec files:[/cyan] {', '.join(config.spec_paths)}")

    # Requirements count
    req_path = Path(repo_root) / ".plumb" / "requirements.json"
    if req_path.exists():
        try:
            reqs = json.loads(req_path.read_text())
            console.print(f"[cyan]Requirements:[/cyan] {len(reqs)}")
        except Exception:
            console.print("[cyan]Requirements:[/cyan] Error reading")
    else:
        console.print("[cyan]Requirements:[/cyan] Not parsed yet")

    # Test count
    test_count = 0
    for tp in config.test_paths:
        test_dir = Path(repo_root) / tp
        if test_dir.is_dir():
            for tf in test_dir.rglob("test_*.py"):
                try:
                    content = tf.read_text()
                    test_count += content.count("def test_")
                except Exception:
                    pass
    console.print(f"[cyan]Tests:[/cyan] {test_count}")

    # Check for uncommitted changes
    has_uncommitted = False
    try:
        from git import Repo
        repo = Repo(repo_root)
        has_uncommitted = repo.is_dirty() or len(repo.untracked_files) > 0
    except Exception:
        pass

    # Decisions
    decisions = read_all_decisions(repo_root)
    pending = [d for d in decisions if d.status == "pending"]
    broken = [d for d in decisions if d.ref_status == "broken"]

    if pending:
        # Group by branch
        by_branch: dict[str, int] = {}
        for d in pending:
            b = d.branch or "unknown"
            by_branch[b] = by_branch.get(b, 0) + 1
        branch_info = ", ".join(f"{b}: {c}" for b, c in by_branch.items())
        console.print(f"[cyan]Pending decisions:[/cyan] {len(pending)} ({branch_info})")
    elif has_uncommitted:
        console.print("[cyan]Pending decisions:[/cyan] 0 [yellow](uncommitted changes — commit to capture decisions)[/yellow]")
    else:
        console.print("[cyan]Pending decisions:[/cyan] 0")

    if broken:
        console.print(f"[red]Broken references:[/red] {len(broken)}")

    console.print(f"[cyan]Last sync commit:[/cyan] {config.last_commit or 'None'}")

    # Coverage summary
    from plumb.coverage_reporter import (
        check_spec_to_test_coverage,
        check_spec_to_code_coverage,
    )
    test_cov, test_total = check_spec_to_test_coverage(repo_root)
    code_cov, code_total = check_spec_to_code_coverage(repo_root)
    if test_total > 0:
        console.print(f"[cyan]Spec-to-test:[/cyan]  {_coverage_bar(test_cov, test_total)}")
    if code_total > 0:
        stale = code_cov == 0 and code_total > 0
        bar = _coverage_bar(code_cov, code_total)
        if stale:
            bar += "  [dim](run plumb coverage to refresh)[/dim]"
        console.print(f"[cyan]Spec-to-code:[/cyan]  {bar}")
