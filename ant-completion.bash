# bash completion for ant (~ANT colony version control)

_ant_completions() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    commands="hatch collect dig log fission tunnel diff splice transplant status deliver gather trail"

    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        return
    fi

    local subcmd="${COMP_WORDS[1]}"

    case "$subcmd" in
        tunnel|splice)
            if [[ $COMP_CWORD -eq 2 && -d .ant/refs ]]; then
                COMPREPLY=( $(compgen -W "$(ls .ant/refs/ 2>/dev/null)" -- "$cur") )
            fi
            ;;
        fission)
            if [[ $COMP_CWORD -eq 2 ]]; then
                # first arg is new branch name — complete existing branches as suggestions
                if [[ -d .ant/refs ]]; then
                    COMPREPLY=( $(compgen -W "$(ls .ant/refs/ 2>/dev/null)" -- "$cur") )
                fi
            elif [[ $COMP_CWORD -eq 3 && -d .ant/refs ]]; then
                # second arg is base branch/chamber
                COMPREPLY=( $(compgen -W "$(ls .ant/refs/ 2>/dev/null)" -- "$cur") )
            fi
            ;;
        collect)
            COMPREPLY=( $(compgen -f -- "$cur") )
            ;;
        transplant)
            # src and dest are paths
            COMPREPLY=( $(compgen -d -- "$cur") )
            ;;
        deliver|gather)
            # host port colony-id password — no useful completion
            ;;
    esac
}

complete -F _ant_completions ant
