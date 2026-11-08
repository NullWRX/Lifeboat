# bash completion for lifeboat
_lifeboat_names() {
    # Job or destination names from the config: [jobs.NAME] / [destinations.NAME]
    sed -n "s/^\[$1\.\([a-z0-9_-]*\)\]\$/\1/p" /etc/lifeboat/config.toml 2>/dev/null
}

_lifeboat() {
    local cur prev words cword
    _init_completion || return
    local cmds="status run start cancel list show browse restore prune pin unpin delete cleanup check drives exports modules detect config schedule history log"
    if (( cword == 1 )); then
        COMPREPLY=($(compgen -W "$cmds --help --version --verbose" -- "$cur"))
        return
    fi
    local cmd=${words[1]}
    if [[ $cur == -* ]]; then
        case $cmd in
            run) COMPREPLY=($(compgen -W "--no-prune --json" -- "$cur")) ;;
            restore) COMPREPLY=($(compgen -W "--to --delete --dry-run --force --yes --json" -- "$cur")) ;;
            prune) COMPREPLY=($(compgen -W "--dry-run --json" -- "$cur")) ;;
            browse) COMPREPLY=($(compgen -W "--meta --json" -- "$cur")) ;;
            pin) COMPREPLY=($(compgen -W "--note" -- "$cur")) ;;
            delete) COMPREPLY=($(compgen -W "--yes" -- "$cur")) ;;
            *) COMPREPLY=($(compgen -W "--json" -- "$cur")) ;;
        esac
        return
    fi
    case $cmd in
        run|start|cancel|list|show|browse|restore|prune|pin|unpin|delete|history|log)
            (( cword == 2 )) && COMPREPLY=($(compgen -W "$(_lifeboat_names jobs)" -- "$cur"))
            [[ $cmd == restore && $prev == --to ]] && _filedir -d
            ;;
        check)
            (( cword == 2 )) && COMPREPLY=($(compgen -W "$(_lifeboat_names destinations)" -- "$cur")) ;;
        config)
            (( cword == 2 )) && COMPREPLY=($(compgen -W "show edit check import example path" -- "$cur"))
            (( cword == 3 )) && _filedir ;;
        schedule)
            (( cword == 2 )) && COMPREPLY=($(compgen -W "sync show cron" -- "$cur")) ;;
    esac
}
complete -F _lifeboat lifeboat
