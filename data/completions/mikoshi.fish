# fish completion for mikoshi
function __mikoshi_names
    sed -n "s/^\[$argv[1]\.\([a-z0-9_-]*\)\]\$/\1/p" /etc/mikoshi/config.toml 2>/dev/null
end

set -l cmds status run start cancel list show browse restore prune pin unpin delete cleanup find-backups adopt check drives exports modules detect config schedule history log
complete -c mikoshi -f
complete -c mikoshi -n "not __fish_seen_subcommand_from $cmds" -a "$cmds"
complete -c mikoshi -n "__fish_seen_subcommand_from run start cancel list show browse restore prune pin unpin delete history log" -a "(__mikoshi_names jobs)"
complete -c mikoshi -n "__fish_seen_subcommand_from check find-backups adopt" -a "(__mikoshi_names destinations)"
complete -c mikoshi -n "__fish_seen_subcommand_from config" -a "show edit check import example path"
complete -c mikoshi -n "__fish_seen_subcommand_from schedule" -a "sync show cron"
complete -c mikoshi -n "__fish_seen_subcommand_from restore" -l to -r -a "(__fish_complete_directories)"
complete -c mikoshi -n "__fish_seen_subcommand_from restore" -l delete -l dry-run -l force -l yes
complete -c mikoshi -n "__fish_seen_subcommand_from prune" -l dry-run
complete -c mikoshi -l json -d "machine-readable output"
