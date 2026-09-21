from __future__ import annotations

import argparse
import json
import sys
import time

from stream_state_router import __version__
from stream_state_router.services.config import (
    ConfigError,
    build_ruleset,
    load_config,
    validate_config,
)
from stream_state_router.services.logging_setup import configure_logging
from stream_state_router.services.recovery import RuntimeMarker
from stream_state_router.services.single_instance import SingleInstanceGuard


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream State Router")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--minimized", action="store_true", help="Démarrer dans la zone de notification")
    parser.add_argument("--headless", action="store_true", help="Afficher le routage dans la console sans interface")
    parser.add_argument("--check-config", action="store_true", help="Valider la configuration puis quitter")
    parser.add_argument(
        "--declarative-coverage",
        action="store_true",
        help="Afficher le rapport read-only de couverture déclarative puis quitter",
    )
    return parser.parse_args(argv)


def run_headless(config: dict) -> int:
    from stream_state_router.router.engine import StateRouterEngine
    from stream_state_router.router.foreground import WindowsForegroundProvider

    rules, poll_ms, debounce_ms, fallback_ms = build_ruleset(config)
    engine = StateRouterEngine(
        rules,
        debounce_ms=debounce_ms,
        fallback_debounce_ms=fallback_ms,
    )
    provider = WindowsForegroundProvider()
    print(f"Stream State Router {__version__} — mode headless")
    print("Ctrl+C pour quitter.\n")
    try:
        while True:
            app = provider.get()
            change = engine.observe(app)
            if change:
                exe = app.exe_name if app else "<none>"
                values = change.current.as_variables()
                print(
                    f"[{change.rule_name}] {exe} -> "
                    + " | ".join(f"{key}={value}" for key, value in values.items())
                )
            time.sleep(max(0.02, poll_ms / 1000.0))
    except KeyboardInterrupt:
        return 0


def run_gui(config: dict, *, minimized: bool, marker: RuntimeMarker) -> int:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print(
            "PySide6 n'est pas installé. Exécutez :\n"
            "  python -m pip install -e .[desktop]\n"
            "ou utilisez la build Windows publiée.",
            file=sys.stderr,
        )
        return 2

    from stream_state_router.ui.main_window import MainWindow
    from stream_state_router.ui.theme import APP_STYLE

    app = QApplication(sys.argv)
    app.setApplicationName("Stream State Router")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(APP_STYLE)
    logger = configure_logging()
    window = MainWindow(
        config,
        logger=logger,
        start_minimized=minimized,
        runtime_marker=marker,
    )
    if not minimized:
        window.show()
    if marker.previous_unclean or marker.previous_cleanup_incomplete:
        logger.warning("Previous session appears to have ended unexpectedly")
        if not minimized:
            QMessageBox.warning(
                window,
                "Session précédente",
                "La session précédente s'est terminée brutalement ou avec un nettoyage OBS incomplet. "
                "SSR tentera de reprendre les obligations de nettoyage compatibles avec la Scene Collection active.",
            )
    code = app.exec()
    return int(code)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.check_config:
        errors = validate_config(config)
        if errors:
            print("Configuration invalide:\n- " + "\n- ".join(errors), file=sys.stderr)
            return 1
        print("Configuration valide.")
        return 0

    if args.declarative_coverage:
        from stream_state_router.planning import build_migration_coverage_report

        report = build_migration_coverage_report(config)
        print(
            json.dumps(
                report.as_mapping(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    guard = SingleInstanceGuard()
    if guard.already_running:
        print("Stream State Router est déjà lancé.", file=sys.stderr)
        guard.close()
        return 3

    marker = RuntimeMarker()
    marker.start()
    try:
        if args.headless:
            return run_headless(config)
        return run_gui(config, minimized=args.minimized, marker=marker)
    finally:
        if not marker.finalized:
            marker.clean_shutdown()
        guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
