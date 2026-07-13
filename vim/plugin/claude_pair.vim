" claude-pair vim integration
" Writes the current file, cursor position, and buffer lines around the
" cursor (including unsaved edits) to a state file that the claude-pair
" watcher reads. No network calls happen from vim.

if exists('g:loaded_claude_pair') || &compatible
  finish
endif
let g:loaded_claude_pair = 1

let g:claude_pair_context_lines = get(g:, 'claude_pair_context_lines', 60)
let g:claude_pair_enabled = get(g:, 'claude_pair_enabled', 1)

let s:cache_home = empty($XDG_CACHE_HOME) ? expand('~/.cache') : $XDG_CACHE_HOME
let s:state_dir = s:cache_home . '/claude-pair'
let s:state_file = s:state_dir . '/vim_state.json'

function! s:WriteState() abort
  if !g:claude_pair_enabled || empty(expand('%')) || !empty(&buftype)
    return
  endif
  if !isdirectory(s:state_dir)
    call mkdir(s:state_dir, 'p')
  endif
  let l:lnum = line('.')
  let l:half = g:claude_pair_context_lines / 2
  let l:first = max([1, l:lnum - l:half])
  let l:last = min([line('$'), l:lnum + l:half])
  let l:state = {
        \ 'file': expand('%:p'),
        \ 'filetype': &filetype,
        \ 'line': l:lnum,
        \ 'col': col('.'),
        \ 'mode': mode(),
        \ 'modified': &modified ? 1 : 0,
        \ 'first_line': l:first,
        \ 'context': getline(l:first, l:last),
        \ 'ts': localtime(),
        \ }
  call writefile([json_encode(l:state)], s:state_file)
endfunction

augroup ClaudePair
  autocmd!
  " CursorHold fires after 'updatetime' ms of idleness; consider
  " `set updatetime=1000` so state stays fresh while you pause.
  autocmd CursorHold,CursorHoldI,BufEnter,BufWritePost,InsertLeave * call s:WriteState()
augroup END

command! ClaudePairToggle let g:claude_pair_enabled = !g:claude_pair_enabled
      \ | echo 'claude-pair vim state: ' . (g:claude_pair_enabled ? 'on' : 'off')
