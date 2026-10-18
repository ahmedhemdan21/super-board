# Sourced first by every eval seed and by evals/run.sh. A paid eval run execs
# extensionless `#!` stubs (gh, git, python3) inside the eval sandbox; neither works on
# Windows, where the run used to die later with a stack trace. Stop early instead.
# Exit 75 (EX_TEMPFAIL: the evals are fine, this machine cannot run them).
# SB_EVAL_REPLAY=1 is set only by tests/test-eval-stubs.sh: its offline seed replay runs
# under plain bash, no sandbox, and passes on Windows.
if [ "${SB_EVAL_REPLAY:-}" != 1 ]; then
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*|Windows_NT)
      echo "skill evals need Linux or macOS (sandbox + exec stubs); run them in WSL or CI" >&2
      exit 75 ;;
  esac
fi
