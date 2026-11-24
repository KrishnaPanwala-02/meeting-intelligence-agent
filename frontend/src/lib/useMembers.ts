import { peopleApi } from '../api/endpoints'
import type { Person } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { useAsync } from './useAsync'

/** Members that can be picked as participants (not yourself: you are always included). */
export function useMembers(): Person[] {
  const { user } = useAuth()
  const people = useAsync(() => peopleApi.list().catch(() => ({ count: 0, people: [] as Person[] })), [])
  const me = (user?.email ?? '').toLowerCase()
  return (people.data?.people ?? []).filter((p) => p.email.toLowerCase() !== me)
}
