from collections import defaultdict
from difflib import SequenceMatcher

from campaigns.models import CampaignLead
from django.db import transaction

from .models import Lead, LeadTag

PUBLIC_EMAIL_DOMAINS = frozenset({
    'gmail.com',
    'googlemail.com',
    'hotmail.com',
    'icloud.com',
    'live.com',
    'outlook.com',
    'proton.me',
    'protonmail.com',
    'yahoo.com',
})
MERGEABLE_FIELDS = frozenset({
    'email',
    'first_name',
    'last_name',
    'company',
    'phone',
    'linkedin_url',
})
STATUS_PRIORITY = {
    'SKIPPED': 0,
    'ENROLLED': 1,
    'PAUSED': 2,
    'ACTIVE': 3,
    'FINISHED': 4,
    'BOUNCED': 5,
    'REPLIED': 6,
}


def _normalized_text(value):
    return ' '.join((value or '').casefold().split())


def _normalized_name(lead):
    return _normalized_text(f'{lead.first_name or ""} {lead.last_name or ""}')


def _email_domain(lead):
    email = (lead.email or '').casefold()
    return email.rsplit('@', 1)[-1] if '@' in email else ''


def _name_block(lead):
    first_name = _normalized_text(lead.first_name)
    last_name = _normalized_text(lead.last_name)
    if not first_name or not last_name:
        return None
    return first_name[0], last_name[:5]


def _profile_completeness(lead):
    fields = ('first_name', 'last_name', 'company', 'phone', 'linkedin_url')
    return sum(bool(getattr(lead, field)) for field in fields)


def find_duplicate_groups(queryset, name_threshold=0.86):
    """Return deterministic duplicate groups without comparing every lead pair."""
    leads = list(queryset.order_by('created_at', 'id'))
    if len(leads) < 2:
        return []

    parent = {lead.id: lead.id for lead in leads}
    edges = []

    def find(lead_id):
        while parent[lead_id] != lead_id:
            parent[lead_id] = parent[parent[lead_id]]
            lead_id = parent[lead_id]
        return lead_id

    def union(left_id, right_id, reason, confidence):
        left_root = find(left_id)
        right_root = find(right_id)
        if left_root != right_root:
            parent[right_root] = left_root
        edges.append((left_id, right_id, reason, confidence))

    domain_buckets = defaultdict(list)
    name_buckets = defaultdict(list)

    for lead in leads:
        domain = _email_domain(lead)
        if domain and domain not in PUBLIC_EMAIL_DOMAINS:
            domain_buckets[domain].append(lead)
        block = _name_block(lead)
        if block:
            name_buckets[block].append(lead)

    for domain_leads in domain_buckets.values():
        anchor = domain_leads[0]
        for candidate in domain_leads[1:]:
            union(anchor.id, candidate.id, 'matching_domain', 0.9)

    compared_pairs = set()
    for block_leads in name_buckets.values():
        for index, left in enumerate(block_leads):
            for right in block_leads[index + 1:]:
                pair = frozenset((left.id, right.id))
                if pair in compared_pairs:
                    continue
                compared_pairs.add(pair)
                similarity = SequenceMatcher(
                    None,
                    _normalized_name(left),
                    _normalized_name(right),
                ).ratio()
                if similarity >= name_threshold:
                    union(left.id, right.id, 'similar_name', round(similarity, 2))

    grouped_leads = defaultdict(list)
    for lead in leads:
        grouped_leads[find(lead.id)].append(lead)

    results = []
    for members in grouped_leads.values():
        if len(members) < 2:
            continue
        member_ids = {member.id for member in members}
        group_edges = [
            edge
            for edge in edges
            if edge[0] in member_ids and edge[1] in member_ids
        ]
        reasons = sorted({edge[2] for edge in group_edges})
        confidence = max((edge[3] for edge in group_edges), default=0)
        suggested_target = max(
            members,
            key=lambda lead: (
                _profile_completeness(lead),
                -lead.created_at.timestamp(),
            ),
        )
        results.append({
            'reasons': reasons,
            'confidence': confidence,
            'suggested_target_id': suggested_target.id,
            'leads': members,
        })

    return sorted(
        results,
        key=lambda group: (-group['confidence'], str(group['suggested_target_id'])),
    )


def _latest_datetime(left, right):
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _earliest_datetime(left, right):
    values = [value for value in (left, right) if value is not None]
    return min(values) if values else None


def _merge_campaign_lead(target_record, source_record):
    target_record.last_opened_at = _latest_datetime(
        target_record.last_opened_at,
        source_record.last_opened_at,
    )
    target_record.last_clicked_at = _latest_datetime(
        target_record.last_clicked_at,
        source_record.last_clicked_at,
    )
    target_record.last_replied_at = _latest_datetime(
        target_record.last_replied_at,
        source_record.last_replied_at,
    )
    target_record.next_execution_time = _earliest_datetime(
        target_record.next_execution_time,
        source_record.next_execution_time,
    )

    if STATUS_PRIORITY.get(source_record.status, -1) > STATUS_PRIORITY.get(
        target_record.status,
        -1,
    ):
        target_record.status = source_record.status

    target_step_order = getattr(target_record.current_step, 'step_order', -1)
    source_step_order = getattr(source_record.current_step, 'step_order', -1)
    if source_step_order > target_step_order:
        target_record.current_step = source_record.current_step

    campaign_fields = (
        'last_sent_message_id',
        'bounce_type',
        'bounce_code',
        'bounce_reason',
    )
    for field in campaign_fields:
        if not getattr(target_record, field) and getattr(source_record, field):
            setattr(target_record, field, getattr(source_record, field))

    target_record.save(update_fields=[
        'last_opened_at',
        'last_clicked_at',
        'last_replied_at',
        'next_execution_time',
        'status',
        'current_step',
        'last_sent_message_id',
        'bounce_type',
        'bounce_code',
        'bounce_reason',
    ])


@transaction.atomic
def merge_leads(organization, target_id, duplicate_ids, field_sources=None):
    """Merge source leads into a target while preserving tenant and campaign history."""
    source_ids = list(dict.fromkeys(duplicate_ids))
    requested_ids = [target_id, *source_ids]
    locked_leads = list(
        Lead.objects.select_for_update()
        .filter(organization=organization, id__in=requested_ids)
        .order_by('created_at', 'id')
    )
    lead_by_id = {lead.id: lead for lead in locked_leads}

    if len(lead_by_id) != len(set(requested_ids)):
        raise Lead.DoesNotExist(
            'One or more selected leads do not belong to this organization.'
        )

    target = lead_by_id[target_id]
    sources = [lead_by_id[source_id] for source_id in source_ids]
    field_sources = field_sources or {}

    selected_values = {}
    for field, source_id in field_sources.items():
        if field not in MERGEABLE_FIELDS or source_id not in lead_by_id:
            raise ValueError(f'Invalid merge source for {field}.')
        selected_values[field] = getattr(lead_by_id[source_id], field)

    merged_custom_data = {}
    merged_custom_variables = {}
    for lead in [*sources, target]:
        merged_custom_data.update(lead.custom_data or {})
        merged_custom_variables.update(lead.custom_variables or {})

    tag_ids = set(
        LeadTag.objects.filter(lead__in=locked_leads).values_list('tag_id', flat=True)
    )
    campaign_records = list(
        CampaignLead.objects.select_for_update()
        .filter(organization=organization, lead__in=locked_leads)
        .select_related('current_step')
        .order_by('created_at', 'id')
    )

    target_records = {
        record.campaign_id: record
        for record in campaign_records
        if record.lead_id == target.id
    }
    moved_count = 0
    collapsed_count = 0

    source_records = [
        record for record in campaign_records if record.lead_id != target.id
    ]
    for source_record in source_records:
        existing_target = target_records.get(source_record.campaign_id)
        if existing_target:
            _merge_campaign_lead(existing_target, source_record)
            source_record.delete()
            collapsed_count += 1
        else:
            source_record.lead = target
            source_record.save(update_fields=['lead'])
            target_records[source_record.campaign_id] = source_record
            moved_count += 1

    for tag_id in tag_ids:
        LeadTag.objects.get_or_create(
            organization=organization,
            lead=target,
            tag_id=tag_id,
        )

    target.custom_data = merged_custom_data
    target.custom_variables = merged_custom_variables
    target.global_unsubscribe = any(lead.global_unsubscribe for lead in locked_leads)
    target.score = max(lead.score for lead in locked_leads)

    for source in sources:
        source.delete()

    for field, value in selected_values.items():
        setattr(target, field, value)

    target.save(update_fields=[
        *sorted(selected_values),
        'custom_data',
        'custom_variables',
        'global_unsubscribe',
        'score',
        'updated_at',
    ])

    return {
        'lead': target,
        'merged_ids': source_ids,
        'campaign_records_moved': moved_count,
        'campaign_records_collapsed': collapsed_count,
    }
