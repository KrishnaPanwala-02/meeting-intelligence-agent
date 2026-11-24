import { useId } from 'react'
import { Link } from 'react-router-dom'
import { foldersApi } from '../api/endpoints'
import type { FolderChoice } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { IconFolder } from './icons'
import { Badge } from './ui'

const NEW = '__new__'

/** Is a "new folder" chosen but left unnamed? Used to stop a submit that would silently skip the folder. */
export function folderChoiceError(choice: FolderChoice): string | null {
  return choice.new_folder !== undefined && choice.new_folder !== null && !choice.new_folder.trim()
    ? 'Name the new folder, or choose “No folder”.'
    : null
}

/** What to send for a choice: nothing, an existing folder, or a new folder by name. */
export function folderFields(choice: FolderChoice): Record<string, string> {
  if (choice.new_folder?.trim()) return { new_folder: choice.new_folder.trim() }
  if (choice.folder_id) return { folder_id: choice.folder_id }
  return {}
}

/**
 * Pick the folder a meeting goes in. Later meetings in a folder remember the earlier ones.
 * "New folder…" shows a box to name it on the spot.
 */
export default function FolderPicker({
  value,
  onChange,
  label = 'Folder',
  help = 'Meetings in a folder remember each other: a new one is summarised knowing what the earlier ones decided and left open.',
}: {
  value: FolderChoice
  onChange: (c: FolderChoice) => void
  label?: string
  help?: string
}) {
  const id = useId()
  const folders = useAsync(() => foldersApi.list().catch(() => ({ count: 0, folders: [] })), [])
  const list = folders.data?.folders ?? []
  const isNew = value.new_folder !== undefined && value.new_folder !== null
  const selected = isNew ? NEW : (value.folder_id ?? '')

  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="row wrap" style={{ gap: '0.6rem' }}>
        <select
          id={id}
          className="input"
          style={{ flex: '1 1 14rem' }}
          value={selected}
          onChange={(e) => {
            const v = e.target.value
            if (v === NEW) onChange({ new_folder: '' })
            else onChange({ folder_id: v || null })
          }}
        >
          <option value="">No folder</option>
          {list.map((f) => (
            <option key={f.id} value={f.id}>
              {f.name} ({f.meeting_count})
            </option>
          ))}
          <option value={NEW}>+ New folder…</option>
        </select>
        {isNew && (
          <input
            className="input"
            style={{ flex: '1 1 14rem' }}
            aria-label="New folder name"
            placeholder="Name the folder, e.g. Zomato project"
            maxLength={80}
            autoFocus
            value={value.new_folder ?? ''}
            onChange={(e) => onChange({ new_folder: e.target.value })}
          />
        )}
      </div>
      {help && <span className="help">{help}</span>}
    </div>
  )
}

/** A small link-chip showing the folder a meeting is in. */
export function FolderBadge({ folder }: { folder: { id: string; name: string; number?: number | null } }) {
  return (
    <Link to={`/folders/${folder.id}`} onClick={(e) => e.stopPropagation()} style={{ textDecoration: 'none' }}>
      <Badge tone="info">
        <IconFolder size={12} /> {folder.number ? `Meeting ${folder.number} · ` : ''}
        {folder.name}
      </Badge>
    </Link>
  )
}
