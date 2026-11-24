import { useCallback, useEffect, useRef, useState } from 'react'

export interface AsyncState<T> {
  data: T | undefined
  error: Error | undefined
  loading: boolean
  reload: () => void
  setData: (updater: (prev: T | undefined) => T | undefined) => void
}

/**
 * Run an async function when `deps` change. Stale results (from a previous
 * dependency set or an unmounted component) are discarded. `reload()` re-runs
 * it, keeping the previous data on screen while the new request is in flight.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setDataState] = useState<T>()
  const [error, setError] = useState<Error>()
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    let live = true
    setLoading(true)
    fnRef
      .current()
      .then((d) => {
        if (!live) return
        setDataState(d)
        setError(undefined)
      })
      .catch((e: unknown) => {
        if (live) setError(e instanceof Error ? e : new Error(String(e)))
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])

  const reload = useCallback(() => setTick((t) => t + 1), [])
  const setData = useCallback(
    (updater: (prev: T | undefined) => T | undefined) => setDataState((p) => updater(p)),
    [],
  )
  return { data, error, loading, reload, setData }
}

/** Call `fn` every `ms` while `active`; cleans up on unmount. */
export function usePoll(fn: () => void | Promise<void>, ms: number, active: boolean) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => void ref.current(), ms)
    return () => clearInterval(id)
  }, [ms, active])
}
