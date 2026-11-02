#!/bin/bash

main() {
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || return 1
    repo_root="$(cd -- "$script_dir/../.." && pwd)" || return 1
    cd -- "$repo_root" || return 1

    if ! "$repo_root/.venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; then
        if [[ -d "$repo_root/.venv" ]]; then
            printf '%s\n' '项目里的 .venv 环境不可用（可能是从别的电脑拷贝过来的）。请删除项目文件夹里的 .venv 文件夹，再重新运行“开始设置”。聊天数据和设置不在 .venv 里，不会丢失。'
            return 1
        fi
        python_bin=""
        for candidate in python3.12 \
            /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
            /opt/homebrew/bin/python3.12; do
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; then
                python_bin="$candidate"
                break
            fi
        done
        if [[ -z "$python_bin" ]]; then
            printf '没有找到 Python 3.12，请先按说明安装\n'
            return 1
        fi

        "$python_bin" -m venv "$repo_root/.venv" || return $?
    fi
    if ! "$repo_root/.venv/bin/python" -m pip install -q -e ".[share]"; then
        printf '安装依赖失败，请检查网络后重试\n'
        return 1
    fi
    "$repo_root/.venv/bin/python" "$repo_root/scripts/setup_share.py" "$@"
}

main "$@"
result=$?
read -r -p "按回车关闭窗口"
printf '\n'
exit "$result"
