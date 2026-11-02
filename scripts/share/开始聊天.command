#!/bin/bash

main() {
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || return 1
    repo_root="$(cd -- "$script_dir/../.." && pwd)" || return 1
    cd -- "$repo_root" || return 1

    if ! "$repo_root/.venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; then
        printf '请先运行“开始设置”\n'
        return 1
    fi
    share_root="$HOME/Library/Application Support/kiki-share"
    export COMPANION_PRIVATE_DATA_DIR="$share_root/private"
    export COMPANION_RUNTIME_DATA_DIR="$share_root/runtime"
    export COMPANION_TEST_RESULTS_DIR="$share_root/test-results"
    "$repo_root/.venv/bin/python" -m companion.runtime.text_chat --project-root "$repo_root"
}

main "$@"
result=$?
read -r -p "按回车关闭窗口"
printf '\n'
exit "$result"
