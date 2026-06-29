@threads_bp.route("/threads/<int:thread_id>/members/add", methods=["POST"])
@token_required
def add_members_to_thread(current_user, thread_id):
    """
    Directly add one or more users to a thread as full members.

    Only the creator or a moderator can call this endpoint.
    Added users must be accepted connections of the current user.
    Already-members are silently skipped (idempotent).
    Capacity is checked before adding; the batch is rejected if it would
    exceed max_members.

    Body JSON:
        { "user_ids": [<int>, ...] }   -- 1–10 user IDs

    Returns:
        added   – list of users successfully added
        skipped – user IDs that were already members or not found
    """
    try:
        thread = Thread.query.get(thread_id)
        if not thread:
            return error_response("Thread not found", 404)

        if not thread.is_open:
            return error_response("Thread is closed — reopen it before adding members", 403)

        membership = ThreadMember.query.filter_by(
            thread_id=thread_id, student_id=current_user.id
        ).first()
        if not membership or membership.role not in ("creator", "moderator"):
            return error_response("Only the creator or a moderator can add members", 403)

        data     = request.get_json(silent=True) or {}
        user_ids = data.get("user_ids", [])

        if not user_ids or not isinstance(user_ids, list):
            return error_response("user_ids must be a non-empty array")
        if len(user_ids) > 10:
            return error_response("Cannot add more than 10 members at once")

        # ── Verify every requested ID is an accepted connection ─────────
        from sqlalchemy import or_, and_
        accepted_connection_ids = {
            (c.receiver_id if c.requester_id == current_user.id else c.requester_id)
            for c in Connection.query.filter(
                or_(
                    and_(
                        Connection.requester_id == current_user.id,
                        Connection.receiver_id.in_(user_ids)
                    ),
                    and_(
                        Connection.receiver_id == current_user.id,
                        Connection.requester_id.in_(user_ids)
                    )
                ),
                Connection.status == "accepted"
            ).all()
        }

        # ── Who is already a member? ─────────────────────────────────────
        existing_member_ids = {
            m.student_id
            for m in ThreadMember.query.filter(
                ThreadMember.thread_id   == thread_id,
                ThreadMember.student_id.in_(user_ids)
            ).all()
        }

        to_add  = []
        skipped = []
        for uid in user_ids:
            if uid == current_user.id:
                skipped.append(uid)
                continue
            if uid in existing_member_ids:
                skipped.append(uid)
                continue
            if uid not in accepted_connection_ids:
                # Not a connection — cannot add
                skipped.append(uid)
                continue
            user = User.query.get(uid)
            if not user or user.status != "approved":
                skipped.append(uid)
                continue
            to_add.append(user)

        if not to_add:
            return error_response(
                "No eligible users to add — they may already be members, "
                "not your connections, or have inactive accounts"
            )

        # ── Capacity check ───────────────────────────────────────────────
        slots_available = thread.max_members - thread.member_count
        if len(to_add) > slots_available:
            return error_response(
                f"Not enough space — {slots_available} slot(s) available, "
                f"but you are trying to add {len(to_add)}"
            )

        # ── Add members ──────────────────────────────────────────────────
        added = []
        for user in to_add:
            # Cancel any existing pending/rejected join-request or invite row
            # so the new direct-add row doesn't violate the unique constraint.
            ThreadJoinRequest.query.filter_by(
                thread_id=thread_id, requester_id=user.id
            ).delete()

            db.session.add(ThreadMember(
                thread_id=thread_id, student_id=user.id, role="member"
            ))
            db.session.add(Notification(
                user_id=user.id,
                title=f"{current_user.name} added you to a thread",
                body=f'You are now a member of "{thread.title}"',
                notification_type="thread_member_added",
                related_type="thread",
                related_id=thread_id
            ))
            added.append({"id": user.id, "username": user.username, "name": user.name, "avatar": user.avatar})

        Thread.query.filter_by(id=thread_id).update(
            {
                Thread.member_count:  Thread.member_count + len(to_add),
                Thread.last_activity: datetime.datetime.utcnow()
            },
            synchronize_session=False
        )

        db.session.commit()

        # ── Real-time notifications ──────────────────────────────────────
        try:
            from services.websocket_threads import thread_ws_manager

            # Tell everyone already in the thread room that new people joined
            for user_data in added:
                thread_ws_manager.broadcast_to_thread(thread_id, "thread_member_joined", {
                    "thread_id": thread_id,
                    "user": user_data,
                })

            # Reload the thread row so member_count is fresh
            thread = Thread.query.get(thread_id)
            thread_snapshot = {
                "id":           thread.id,
                "title":        thread.title,
                "avatar":       thread.avatar,
                "description":  thread.description,
                "department":   thread.department,
                "tags":         thread.tags or [],
                "member_count": thread.member_count,
                "max_members":  thread.max_members,
                "is_open":      thread.is_open,
                "last_activity": thread.last_activity.isoformat(),
                "your_role":    "member",
                "unread_count": 0,
            }

            # Push thread_joined to each new member's personal room so the
            # thread appears in their list immediately without a reload.
            for user_data in added:
                thread_ws_manager.notify_user(user_data["id"], "thread_joined", {
                    "thread_id": thread_id,
                    "thread":    thread_snapshot,
                })

        except Exception as ws_err:
            current_app.logger.warning(
                f"[ADD_MEMBERS_WS_FAILED] thread_id={thread_id} error={ws_err!r}"
            )

        return jsonify({
            "status":  "success",
            "message": f"Added {len(added)} member(s) to the thread",
            "data": {
                "added":       added,
                "skipped":     skipped,
                "member_count": thread.member_count,
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Add members to thread error: {e!r}", exc_info=True)
        return error_response("Failed to add members")