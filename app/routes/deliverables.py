from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.firebase_config import db
from app.decorators import login_required, role_required
from app.routes.events import get_event_or_403
from google.cloud.firestore import SERVER_TIMESTAMP

deliverables_bp = Blueprint('deliverables', __name__,
    url_prefix='/organizer/events/<event_id>/sponsors/<sponsor_id>/deliverables')

def get_sponsor_or_404(event_id, sponsor_id):
    """Helper to fetch sponsor document."""
    sponsor_ref = db.collection('events').document(event_id)\
                    .collection('sponsors').document(sponsor_id)
    sponsor_doc = sponsor_ref.get()
    if not sponsor_doc.exists:
        return None, None
    return sponsor_ref, {**sponsor_doc.to_dict(), 'id': sponsor_doc.id}

@deliverables_bp.route('/')
@login_required
@role_required('organizer')
def list_deliverables(event_id, sponsor_id):
    doc, event_data = get_event_or_403(event_id)
    if not doc:
        return redirect(url_for('events.list_events'))

    sponsor_ref, sponsor_data = get_sponsor_or_404(event_id, sponsor_id)
    if not sponsor_data:
        flash('Sponsor not found.', 'danger')
        return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

    deliverables_docs = sponsor_ref.collection('deliverables').stream()
    deliverables = [{**d.to_dict(), 'id': d.id} for d in deliverables_docs]

    # Count by status
    total      = len(deliverables)
    completed  = sum(1 for d in deliverables if d.get('status') == 'completed')
    in_progress = sum(1 for d in deliverables if d.get('status') == 'in_progress')
    pending    = sum(1 for d in deliverables if d.get('status') == 'pending')
    progress_pct = int((completed / total) * 100) if total > 0 else 0

    return render_template(
        'organizer/deliverables/list.html',
        event=event_data,
        event_id=event_id,
        sponsor=sponsor_data,
        sponsor_id=sponsor_id,
        deliverables=deliverables,
        total=total,
        completed=completed,
        in_progress=in_progress,
        pending=pending,
        progress_pct=progress_pct
    )

@deliverables_bp.route('/add', methods=['POST'])
@login_required
@role_required('organizer')
def add_deliverable(event_id, sponsor_id):
    sponsor_ref, sponsor_data = get_sponsor_or_404(event_id, sponsor_id)
    if not sponsor_data:
        flash('Sponsor not found.', 'danger')
        return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

    title       = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()

    if not title:
        flash('Deliverable title is required.', 'danger')
        return redirect(url_for('deliverables.list_deliverables',
                                event_id=event_id, sponsor_id=sponsor_id))

    sponsor_ref.collection('deliverables').add({
        'title':       title,
        'description': description,
        'status':      'pending',
        'created_at':  SERVER_TIMESTAMP
    })

    flash(f'Deliverable "{title}" added!', 'success')
    return redirect(url_for('deliverables.list_deliverables',
                            event_id=event_id, sponsor_id=sponsor_id))

@deliverables_bp.route('/<deliverable_id>/update_status', methods=['POST'])
@login_required
@role_required('organizer')
def update_status(event_id, sponsor_id, deliverable_id):
    sponsor_ref, sponsor_data = get_sponsor_or_404(event_id, sponsor_id)
    if not sponsor_data:
        flash('Sponsor not found.', 'danger')
        return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

    new_status = request.form.get('status')
    if new_status not in ['pending', 'in_progress', 'completed']:
        flash('Invalid status.', 'danger')
        return redirect(url_for('deliverables.list_deliverables',
                                event_id=event_id, sponsor_id=sponsor_id))

    sponsor_ref.collection('deliverables').document(deliverable_id)\
               .update({'status': new_status})

    flash('Deliverable status updated!', 'success')
    return redirect(url_for('deliverables.list_deliverables',
                            event_id=event_id, sponsor_id=sponsor_id))

@deliverables_bp.route('/<deliverable_id>/delete', methods=['POST'])
@login_required
@role_required('organizer')
def delete_deliverable(event_id, sponsor_id, deliverable_id):
    sponsor_ref, sponsor_data = get_sponsor_or_404(event_id, sponsor_id)
    if not sponsor_data:
        flash('Sponsor not found.', 'danger')
        return redirect(url_for('sponsors.list_sponsors', event_id=event_id))

    sponsor_ref.collection('deliverables').document(deliverable_id).delete()
    flash('Deliverable deleted.', 'info')
    return redirect(url_for('deliverables.list_deliverables',
                            event_id=event_id, sponsor_id=sponsor_id))