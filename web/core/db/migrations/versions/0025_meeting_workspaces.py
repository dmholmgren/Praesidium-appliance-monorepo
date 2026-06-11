"""meeting workspaces and collaborative components

Revision ID: 0025_meeting_workspaces
Revises: 0024_matter_project_tags
Create Date: 2026-05-17

Meeting workspace system:
  - meeting_workspaces: auto-created from calendar events or ad hoc
  - workspace_pages: tldraw canvases, notes, agendas — ordered pages in a notebook
  - workspace_documents: DMS document references dragged into a workspace
  - workspace_participants: who was in the workspace session
  - workspace_snapshots: PDF exports of whiteboard/notes pages → DMS artifacts
  - meeting_sessions: conferencing sessions (LiveKit native, Zoom SDK, etc.)

Widget registry additions:
  - workspace category: whiteboard, document_panel, notes, video_pip, 
    meeting_agenda, participant_list, transcript_live, action_items
  - calendar category additions: matter_calendar, meeting_workspace_launcher
  - collaboration category: notebook_launcher, workspace_recall
"""

revision = '0025_meeting_workspaces'
down_revision = '0024_matter_project_tags'
branch_labels = None
depends_on = None


def upgrade():
    # DDL executed directly via psql on host
    pass


def downgrade():
    from alembic import op
    op.execute("DELETE FROM widget_registry WHERE category IN ('workspace', 'collaboration')")
    op.execute("DELETE FROM widget_registry WHERE widget_slug IN ('calendar_matter_calendar', 'calendar_meeting_workspace')")
    op.drop_table('workspace_snapshots')
    op.drop_table('workspace_participants')
    op.drop_table('workspace_documents')
    op.drop_table('workspace_pages')
    op.drop_table('meeting_sessions')
    op.drop_table('meeting_workspaces')
