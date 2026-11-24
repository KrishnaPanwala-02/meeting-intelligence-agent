import { Card, EmptyState, LinkButton } from '../components/ui'
import { IconAlert } from '../components/icons'

export default function NotFoundPage() {
  return (
    <Card>
      <EmptyState icon={<IconAlert />} title="Page not found" action={<LinkButton variant="primary" to="/">Go to dashboard</LinkButton>}>
        The page you are looking for does not exist.
      </EmptyState>
    </Card>
  )
}
