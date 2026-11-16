import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api, getUserId, setUserId } from './api.js'

const Ctx = createContext(null)

export function UserProvider({ children }) {
  const [userId, setUser] = useState(getUserId())
  const [meta, setMeta] = useState(null)
  const [version, setVersion] = useState(0) // bump to refetch pages after a profile change

  useEffect(() => {
    api.meta().then(setMeta).catch(() => setMeta(null))
  }, [])

  const switchUser = useCallback((id) => {
    setUserId(id)
    setUser(id)
    setVersion((v) => v + 1)
  }, [])

  const refresh = useCallback(() => setVersion((v) => v + 1), [])

  return <Ctx.Provider value={{ userId, switchUser, meta, version, refresh }}>{children}</Ctx.Provider>
}

export const useUser = () => useContext(Ctx)

// Small data-fetching hook: re-runs when deps or the global version change.
export function useApi(fn, deps = []) {
  const { version } = useUser()
  const [state, setState] = useState({ data: null, error: null, loading: true })
  useEffect(() => {
    let alive = true
    setState((s) => ({ ...s, loading: true, error: null }))
    fn()
      .then((data) => alive && setState({ data, error: null, loading: false }))
      .catch((error) => alive && setState({ data: null, error, loading: false }))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, ...deps])
  return state
}
