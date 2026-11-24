import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'
import Layout from './components/Layout'
import { Loading } from './components/ui'
import { ForgotPage, LoginPage, RegisterPage, ResetPage } from './pages/AuthPages'
import AnalyticsPage from './pages/AnalyticsPage'
import AskPage from './pages/AskPage'
import CalendarPage from './pages/CalendarPage'
import DashboardPage from './pages/DashboardPage'
import MeetingDetailPage from './pages/MeetingDetailPage'
import FoldersPage from './pages/FoldersPage'
import FolderDetailPage from './pages/FolderDetailPage'
import MeetingsPage from './pages/MeetingsPage'
import NewMeetingPage from './pages/NewMeetingPage'
import NotFoundPage from './pages/NotFoundPage'
import SearchPage from './pages/SearchPage'
import SettingsPage from './pages/SettingsPage'
import TasksPage from './pages/TasksPage'

function Protected({ children }: { children: JSX.Element }) {
  const { status } = useAuth()
  const loc = useLocation()
  if (status === 'loading') return <Loading label="Restoring your session…" />
  if (status === 'anon') return <Navigate to="/login" replace state={{ from: loc.pathname + loc.search }} />
  return children
}

function PublicOnly({ children }: { children: JSX.Element }) {
  const { status } = useAuth()
  if (status === 'loading') return <Loading />
  if (status === 'authed') return <Navigate to="/" replace />
  return children
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<PublicOnly><LoginPage /></PublicOnly>} />
      <Route path="/register" element={<PublicOnly><RegisterPage /></PublicOnly>} />
      <Route path="/forgot-password" element={<PublicOnly><ForgotPage /></PublicOnly>} />
      <Route path="/reset-password" element={<ResetPage />} />

      <Route element={<Protected><Layout /></Protected>}>
        <Route index element={<DashboardPage />} />
        <Route path="meetings" element={<MeetingsPage />} />
        <Route path="meetings/new" element={<NewMeetingPage />} />
        <Route path="meetings/:id" element={<MeetingDetailPage />} />
        <Route path="folders" element={<FoldersPage />} />
        <Route path="folders/:id" element={<FolderDetailPage />} />
        <Route path="tasks" element={<TasksPage />} />
        <Route path="ask" element={<AskPage />} />
        <Route path="search" element={<SearchPage />} />
        <Route path="analytics" element={<AnalyticsPage />} />
        <Route path="calendar" element={<CalendarPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
