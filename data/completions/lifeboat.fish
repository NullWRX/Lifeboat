# fish completion for lifeboat
function __lifeboat_names
    sed -n "s/^\[$argv[1]\.\([a-z0-9_-]*\)\]\$/\1/p" /etc/lifeboat/config.toml 2>/dev/null
end

set -l cmds status run start cancel list show browse restore prune pin unpin delete cleanup check drives exports modules detect config schedule history log
complete -c lifeboat -f
complete -c lifeboat -n "not __fish_seen_subcommand_from $cmds" -a "$cmds"
complete -c lifeboat -n "__fish_seen_subcommand_from run start cancel list show browse restore prune pin unpin delete history log" -a "(__lifeboat_names jobs)"
complete -c lifeboat -n "__fish_seen_subcommand_from check" -a "(__lifeboat_names destinations)"
complete -c lifeboat -n "__fish_seen_subcommand_from config" -a "show edit check import example path"
complete -c lifeboat -n "__fish_seen_subcommand_from schedule" -a "sync show cron"
complete -c lifeboat -n "__fish_seen_subcommand_from restore" -l to -r -a "(__fish_complete_directories)"
complete -c lifeboat -n "__fish_seen_subcommand_from restore" -l delete -l dry-run -l force -l yes
complete -c lifeboat -n "__fish_seen_subcommand_from prune" -l dry-run
complete -c lifeboat -l json -d "machine-readable output"
