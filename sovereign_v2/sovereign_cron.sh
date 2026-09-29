#!/bin/bash
# Sovereign cron entrypoint. Secrets are SOURCED from .envrc next to this
# script, never written here. Example crontab (times US/Pacific, Mon-Fri):
#   0 6 * * 1-5      /path/to/sovereign_v2/sovereign_cron.sh congress
#   */30 6-12 * * 1-5 /path/to/sovereign_v2/sovereign_cron.sh manage
#   55 12 * * 1-5    /path/to/sovereign_v2/sovereign_cron.sh manage
#   30 6-12 * * 1-5  /path/to/sovereign_v2/sovereign_cron.sh scan
#   50 6,10 * * 1-5  /path/to/sovereign_v2/sovereign_cron.sh execute
#   5 13 * * 1-5     /path/to/sovereign_v2/sovereign_cron.sh dashboard
#   0 8 * * 0        /path/to/sovereign_v2/sovereign_cron.sh congress-score
#   0 9 * * 6        /path/to/sovereign_v2/sovereign_cron.sh backtest
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${SOVEREIGN_PYTHON:-python3}"
LOG=$DIR/sovereign_results/cron.log

set -a
[ -f "$DIR/.envrc" ] && . "$DIR/.envrc"
set +a
export ALPACA_PAPER="${ALPACA_PAPER:-true}"
# Thesis backend: auto (SDK if ANTHROPIC_API_KEY is set, else the Claude CLI),
# cli (Claude CLI with your own login), or sdk.
export SOVEREIGN_THESIS_BACKEND="${SOVEREIGN_THESIS_BACKEND:-auto}"

mkdir -p "$DIR/sovereign_results" "$DIR/sovereign_state"
cd "$DIR" || exit 1

case "$1" in
    scan)
        echo "$(date): Running main scan" >> $LOG
        $VENV sovereign_pipeline.py scan >> $LOG 2>&1
        ;;
    opportunity)
        echo "$(date): Running opportunity scan" >> $LOG
        $VENV sovereign_opportunity.py >> $LOG 2>&1
        ;;
    portfolio)
        echo "$(date): Portfolio check" >> $LOG
        $VENV sovereign_pipeline.py portfolio >> $LOG 2>&1
        ;;
    congress)
        echo "$(date): Congressional trading pull" >> $LOG
        $VENV congress_scraper.py pull --max-pdfs 30 >> $LOG 2>&1
        $VENV congress_scraper.py herd >> $LOG 2>&1
        ;;
    congress-score)
        echo "$(date): Rebuilding member track records" >> $LOG
        $VENV member_scoring.py build >> $LOG 2>&1
        ;;
    execute)
        echo "$(date): Executing on latest signals" >> $LOG
        $VENV sovereign_execute.py execute >> $LOG 2>&1
        ;;
    manage)
        echo "$(date): Managing positions (stops/targets)" >> $LOG
        $VENV sovereign_execute.py manage >> $LOG 2>&1
        ;;
    dashboard)
        echo "$(date): Regenerating dashboard" >> $LOG
        $VENV sovereign_dashboard.py >> $LOG 2>&1
        ;;
    backtest)
        echo "$(date): Weekly congress backtest" >> $LOG
        $VENV sovereign_backtest.py congress >> $LOG 2>&1
        ;;
    *)
        echo "Usage: sovereign_cron.sh {scan|opportunity|portfolio|congress|congress-score|execute|manage|dashboard|backtest}"
        ;;
esac
