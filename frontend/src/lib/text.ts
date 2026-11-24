export function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** How many times `find` occurs in the given texts (mirrors the server's matching rules). */
export function countMatches(texts: string[], find: string, matchCase: boolean, wholeWord: boolean): number {
  const needle = find.split(/\s+/).filter(Boolean).join(' ')
  if (!needle) return 0
  const body = escapeRegExp(needle)
  const re = new RegExp(wholeWord ? `(?<![\\p{L}\\p{N}_])${body}(?![\\p{L}\\p{N}_])` : body, matchCase ? 'gu' : 'giu')
  let n = 0
  for (const t of texts) n += (t.match(re) ?? []).length
  return n
}

/** `text` cut into pieces, flagging the ones that match any of the search terms (case-insensitive). */
export function highlightTerms(text: string, terms: string[]): { text: string; match: boolean }[] {
  const usable = terms.map((t) => t.trim()).filter(Boolean).sort((a, b) => b.length - a.length)
  if (!usable.length) return [{ text, match: false }]
  const re = new RegExp(`(${usable.map(escapeRegExp).join('|')})`, 'gi')
  return text
    .split(re)
    .filter((p) => p !== '')
    .map((p) => ({ text: p, match: usable.some((t) => t.toLowerCase() === p.toLowerCase()) }))
}
