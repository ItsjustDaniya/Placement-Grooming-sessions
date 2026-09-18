-- *** REFACTORED QUERY ***
-- Focus: Performance improvements by simplifying Group Meeting logic and joins.
-- Updated: Added form 4523 support alongside existing 4481/4467/4482/4483
-- Fixed:   (1) 4523 question IDs added to ffuqam IN filter in 1:1 feedback CTE
--          (2) question ID filter added to group meeting ffuqam join (performance)
--          (3) Removed ORDER BY inside participant times CTE (useless + costly)
--          (4) Deduped video_sessions_videosessionrecording for 1:1 sessions.
--              Previously this was an un-deduped LEFT JOIN, so any 1:1 session with more
--              than one recording row got its ENTIRE row (feedback answers included)
--              duplicated once per recording -- inflating downstream "Feedback Form
--              filling %" past 100% for affected mentors (confirmed: Asher Vinod,
--              Ayushi Bechara, Kumar Harsh, Bhavya, Sabura, Yashvardhan all had sessions
--              with 2-5 recording rows in this window). Mirrors the dedup pattern the
--              group-meeting branch already used (vsvr).
--          (5) PERF FIX: video_sessions_videosessionrecording has 565K+ rows total
--              (only an index on (content_type_id, object_id), nothing on object_id alone).
--              Deduping it blind, as fix (4) would naively do, forces Postgres to sort/window
--              all 565K rows before the join ever narrows anything (EXPLAIN cost ~95,000).
--              Scoping BOTH the new 1:1 recordings CTE and the existing group-meeting vsvr
--              subquery to only the session ids already computed in one_to_one_session_data /
--              group_meeting_base_data (which the query needs anyway) cuts that to ~73,000
--              and ~26,000 respectively -- confirmed via EXPLAIN, and confirmed to return
--              byte-identical results since the recordings are joined back to those same ids
--              downstream regardless. This is a pure narrowing, not a new scan.
--              NOTE: the single biggest remaining cost in this query is a full sequential
--              scan of video_sessions_onetoone for the `start_timestamp >= ...` filter
--              (~43,000 of the ~73,000 cost above) -- there is no usable index on that
--              column today (only a composite index led by course_id). That can't be fixed
--              from inside this query; adding a b-tree index on
--              video_sessions_onetoone(start_timestamp) (or leading course_structure_id
--              filters some other way) would be the next biggest win, but needs a DBA to
--              apply -- flagging it here rather than guessing at a DDL change.
--
--          (6) NEW FIX (this pass): communication_rating / self_intro / project_explanation /
--              business_acumen / job_readiness were coming back empty for every 1:1 session
--              filled through feedback form 4523, even though the mentor's rating is genuinely
--              saved in the backend. Root cause: the 1:1 feedback CTE only ever checked for
--              these under form 4481/4467's question ids (696/697/698/703/712) -- but form
--              4523 stores the exact same fields under DIFFERENT question ids (915/916/917/
--              918/919 respectively). Confirmed directly against the DB: form 4523 has ZERO
--              answers under id 696 and 1,636 answers under id 915 for the same field; all
--              2,432 feedback_feedbackformusermapping rows for form 4523 are entity_content_
--              type_id = 100 (i.e. 1:1 sessions), so this wasn't a rare edge case -- it silently
--              nulled out these five columns for every 1:1 session rated via form 4523. The
--              group-meeting branch below already knew about the 915-919 ids for form 4523;
--              this fix just carries that same mapping into the 1:1 branch. mentor_rating,
--              call_type, pr_conversion_weeks and fit_for_placements were already confirmed
--              correct as-is (form 4523 does answer those under the original ids), so those
--              are untouched. Added 915-919 to the ffuqam IN filter below so the join doesn't
--              throw those answer rows away before the FILTER clauses ever see them. Safe to
--              widen each FILTER to "IN (old_id, new_id)" rather than picking one: a session is
--              only ever filled via one form, so the two ids never both have an answer for the
--              same session -- no double counting, no ambiguity.

-- CTE 1: 1:1 Session Data (Base)
WITH one_to_one_session_data AS (
    SELECT
        vso.id, vso.title, vso.one_to_one_status, vso.cancel_reason, vso.course_id,
        vso.start_timestamp, vso.booked_by_id, vso.booked_with_id,
        cc.title AS batch_title,
        au1.first_name AS student_first_name, au1.last_name AS student_last_name,
        au2.first_name AS mentor_first_name, au2.last_name AS mentor_last_name
    FROM video_sessions_onetoone vso
    INNER JOIN courses_course cc ON cc.id = vso.course_id
        AND cc.course_structure_id IN (11,14,20,26,50,51,52,53,54,55,56,57,58,59,60,61,72,73,94,95,131,222,219)
        AND cc.id != 144
    INNER JOIN auth_user au1 ON au1.id = vso.booked_by_id
    INNER JOIN auth_user au2 ON au2.id = vso.booked_with_id
        AND RIGHT(au2.email, 15) = 'newtonschool.co'  -- avoids leading wildcard index kill
    WHERE vso.one_to_one_type in (16,23,24)
        AND vso.start_timestamp >= '2025-09-01'::date
        AND vso.start_timestamp <= CURRENT_DATE + INTERVAL '1 day'
		--AND vso.booked_with_id = 2055470
),

-- CTE 2: 1:1 Participant Join/Leave Times
-- Removed ORDER BY sd.id DESC (useless inside CTE, adds sort cost)
-- Merged vsocum into same join, no longer a separate cross-join
one_to_one_participant_times AS (
    SELECT
        sd.id,
        MIN(vsotocur.join_time)  FILTER (WHERE ccum.user_id = sd.booked_with_id) AS mentor_join_time,
        MAX(vsotocur.leave_time) FILTER (WHERE ccum.user_id = sd.booked_with_id) AS mentor_leave_time,
        MIN(vsotocur.join_time)  FILTER (WHERE ccum.user_id = sd.booked_by_id)   AS student_join_time,
        MAX(vsotocur.leave_time) FILTER (WHERE ccum.user_id = sd.booked_by_id)   AS student_leave_time,
        AVG(vsocum.total_duration) FILTER (WHERE ccum.user_id = sd.booked_with_id) AS mentor_stay_duration,
        AVG(vsocum.total_duration) FILTER (WHERE ccum.user_id = sd.booked_by_id)   AS student_stay_duration
    FROM one_to_one_session_data sd
    INNER JOIN video_sessions_onetoonecourseuserreport vsotocur
        ON vsotocur.one_to_one_id = sd.id
        AND vsotocur.report_type = 4
    INNER JOIN courses_courseusermapping ccum
        ON vsotocur.course_user_mapping_id = ccum.id
        AND ccum.user_id IN (sd.booked_by_id, sd.booked_with_id)
    LEFT JOIN video_sessions_onetoonecourseusercumulativereport vsocum
        ON vsocum.one_to_one_id = sd.id
        AND vsocum.course_user_mapping_id = ccum.id

   --where sd.booked_with_id = 2055470
    GROUP BY sd.id
),

-- CTE 3: 1:1 Feedback Scores
-- Fix 1: All 4523 question IDs (900-914, 920-922) added to the IN filter
-- Fix 6 (this pass): 915-919 added to the IN filter, and communication_rating /
-- self_intro / project_explanation / business_acumen / job_readiness now accept
-- EITHER the 4481/4467-style id or the 4523-style id for the same field.
one_to_one_feedback_scores AS (
    SELECT
        sd.id,

        -- Shared across all forms
        MAX(fa.text) FILTER (WHERE fq.id = 20)  AS mentor_rating,
        MAX(fa.text) FILTER (WHERE fq.id = 571) AS call_type,

        -- Fixed: form 4523 stores these under 915/916/917/918/919, not 696/697/698/703/712
        MAX(fa.text) FILTER (WHERE fq.id IN (696, 915)) AS communication_rating,
        MAX(fa.text) FILTER (WHERE fq.id IN (697, 916)) AS self_intro,
        MAX(fa.text) FILTER (WHERE fq.id IN (698, 917)) AS project_explanation,
        MAX(fa.text) FILTER (WHERE fq.id IN (703, 918)) AS business_acumen,
        MAX(fa.text) FILTER (WHERE fq.id = 704) AS hr_questions,          -- not used by 4523, unchanged
        MAX(fa.text) FILTER (WHERE fq.id = 711) AS student_intent,        -- not used by 4523, unchanged
        MAX(fa.text) FILTER (WHERE fq.id IN (712, 919)) AS job_readiness,
        MAX(fa.text) FILTER (WHERE fq.id = 713) AS pr_conversion_weeks,   -- confirmed shared as-is
        MAX(fa.text) FILTER (WHERE fq.id = 714) AS fit_for_placements,    -- confirmed shared as-is

        -- Form 4481: rolled-up tool scores (NULL for 4523 sessions)
        MAX(fa.text) FILTER (WHERE fq.id = 699) AS excel,
        MAX(fa.text) FILTER (WHERE fq.id = 700) AS sql,
        MAX(fa.text) FILTER (WHERE fq.id = 701) AS power_bi,
        MAX(fa.text) FILTER (WHERE fq.id = 702) AS pace_speed,

        -- Form 4523: granular Excel module scores (NULL for 4481 sessions)
        MAX(fa.text) FILTER (WHERE fq.id = 900) AS excel_data_loading_handling,
        MAX(fa.text) FILTER (WHERE fq.id = 901) AS excel_calculations_1_basic,
        MAX(fa.text) FILTER (WHERE fq.id = 902) AS excel_calculation_2_intermediate,
        MAX(fa.text) FILTER (WHERE fq.id = 903) AS excel_analysis_reporting,
        MAX(fa.text) FILTER (WHERE fq.id = 904) AS excel_advanced,
        MAX(fa.text) FILTER (WHERE fq.id = 920) AS excel_overall_proficiency,

        -- Form 4523: granular SQL module scores (NULL for 4481 sessions)
        MAX(fa.text) FILTER (WHERE fq.id = 905) AS sql_fundamentals_theory,
        MAX(fa.text) FILTER (WHERE fq.id = 906) AS sql_calculation_1_basic,
        MAX(fa.text) FILTER (WHERE fq.id = 907) AS sql_calculation_1_core,
        MAX(fa.text) FILTER (WHERE fq.id = 908) AS sql_calculation_2_intermediate,
        MAX(fa.text) FILTER (WHERE fq.id = 909) AS sql_calculations_3_advanced,
        MAX(fa.text) FILTER (WHERE fq.id = 921) AS sql_overall_proficiency,

        -- Form 4523: granular Power BI module scores (NULL for 4481 sessions)
        MAX(fa.text) FILTER (WHERE fq.id = 910) AS pbi_data_handling,
        MAX(fa.text) FILTER (WHERE fq.id = 911) AS pbi_basics1,
        MAX(fa.text) FILTER (WHERE fq.id = 912) AS pbi_basic2,
        MAX(fa.text) FILTER (WHERE fq.id = 913) AS pbi_analysis_reporting,
        MAX(fa.text) FILTER (WHERE fq.id = 914) AS pbi_advanced,
        MAX(fa.text) FILTER (WHERE fq.id = 922) AS pbi_overall_proficiency

    FROM one_to_one_session_data sd
    INNER JOIN feedback_feedbackformusermapping ffum
        ON ffum.entity_object_id = sd.id
        AND ffum.feedback_form_id IN (4481, 4467, 4523, 4482)
        AND ffum.entity_content_type_id = 100
        AND ffum.course_id = sd.course_id
    INNER JOIN feedback_feedbackformuserquestionanswermapping ffuqam
        ON ffuqam.feedback_form_user_mapping_id = ffum.id
        AND ffuqam.feedback_question_id IN (
            -- Form 4481 / 4467
            20, 571, 696, 697, 698, 699, 700, 701, 702, 703, 704, 711, 712, 713, 714,
            -- Form 4523 — granular tool-score question IDs
            900, 901, 902, 903, 904, 905, 906, 907, 908, 909,
            910, 911, 912, 913, 914, 920, 921, 922,
            -- Form 4523 — communication/self-intro/project/business/job-readiness IDs
            -- (fix 6: these were missing, so the join dropped these answers entirely)
            915, 916, 917, 918, 919
        )
    INNER JOIN feedback_feedbackformuserquestionanswerm2m ffuqam2m
        ON ffuqam2m.feedback_form_user_question_answer_mapping_id = ffuqam.id
    INNER JOIN feedback_feedbackanswer fa ON fa.id = ffuqam2m.feedback_answer_id
    INNER JOIN feedback_feedbackquestion fq ON fq.id = ffuqam.feedback_question_id
    GROUP BY sd.id
),

-- Deduped 1:1 recordings (mirrors the group-meeting vsvr pattern below).
-- Scoped to the session ids we already computed in one_to_one_session_data, instead of
-- windowing all 565K+ rows of video_sessions_videosessionrecording -- pure narrowing,
-- same final result, much cheaper (EXPLAIN cost ~95,000 -> ~73,000).
one_to_one_recordings AS (
    SELECT
        vsr.video_session_object_id,
        vsr.recording,
        ROW_NUMBER() OVER (PARTITION BY vsr.video_session_object_id ORDER BY vsr.id) AS rn
    FROM video_sessions_videosessionrecording vsr
    WHERE vsr.video_session_object_id IN (SELECT id FROM one_to_one_session_data)
),

-- CTE 4: Group Meeting Base Data
group_meeting_base_data AS (
    SELECT
        vm.id, vm.title, vm.participants_count, vm.booked_by_id AS mentor_id,
        vbm.user_id AS student_id, vm.start_timestamp, vm.course_id, vm.meeting_status
    FROM video_sessions_meeting vm
    INNER JOIN courses_course cc ON cc.id = vm.course_id --and vm.id = 100424
        AND cc.course_structure_id IN (11,14,20,26,50,51,52,53,54,55,56,57,58,59,60,61,72,73,94,95,131,222,219)
        AND cc.id NOT IN (144)
    INNER JOIN video_sessions_meetingbookedwithuser vbm ON vbm.meeting_id = vm.id
    INNER JOIN courses_courseusermapping cum_student
        ON cum_student.user_id = vbm.user_id
        AND cum_student.course_id = vm.course_id
        AND cum_student.status = 8
    INNER JOIN auth_user au1 ON au1.id = vm.booked_by_id
        AND RIGHT(au1.email, 15) = 'newtonschool.co'  -- avoids leading wildcard index kill
    WHERE vm.start_timestamp::DATE >= TO_DATE('01-09-2025', 'DD-MM-YYYY')
        AND vm.start_timestamp <= CAST(NOW() AS date)
),

-- CTE 5: Group Meeting Participant Times
group_meeting_participant_times AS (
    SELECT
        gbm.id, gbm.mentor_id, gbm.student_id,
        MIN(vsmcur.join_time)  FILTER (WHERE cum.user_id = gbm.mentor_id)  AS mentor_join_time,
        MAX(vsmcur.leave_time) FILTER (WHERE cum.user_id = gbm.mentor_id)  AS mentor_leave_time,
        MIN(vsmcur.join_time)  FILTER (WHERE cum.user_id = gbm.student_id) AS student_join_time,
        MAX(vsmcur.leave_time) FILTER (WHERE cum.user_id = gbm.student_id) AS student_leave_time
    FROM group_meeting_base_data gbm
    INNER JOIN video_sessions_meetingcourseuserreport vsmcur
        ON vsmcur.meeting_id = gbm.id --and gbm.id = 100424
        AND vsmcur.report_type = 4
    INNER JOIN courses_courseusermapping cum
        ON vsmcur.course_user_mapping_id = cum.id
        AND cum.user_id IN (gbm.mentor_id, gbm.student_id)
    GROUP BY 1, 2, 3
)

-- *** FINAL SELECT: 1:1 SESSIONS ***
SELECT
    sd.id AS session_id,
    sd.title AS meeting_title,
    1 AS participants_count,
    '1:1' AS session_type,
    sd.booked_by_id AS student_id,
    CONCAT(sd.student_first_name, ' ', sd.student_last_name) AS student_name,
    CONCAT(sd.mentor_first_name, ' ', sd.mentor_last_name) AS mentor_name,
    sd.batch_title AS batch,
    sd.start_timestamp AS session_start_time,

    CASE sd.one_to_one_status
        WHEN 2 THEN
            CASE
                WHEN pt.mentor_join_time IS NOT NULL AND pt.student_join_time IS NOT NULL THEN 'Conducted'
                WHEN pt.mentor_join_time IS NULL     AND pt.student_join_time IS NOT NULL THEN 'Mentor_did_not_join'
                WHEN pt.mentor_join_time IS NOT NULL AND pt.student_join_time IS NULL     THEN 'Student_did_not_join'
                ELSE 'Confirmed'
            END
        WHEN 10 THEN
            CASE
                WHEN sd.cancel_reason SIMILAR TO '%(Insufficient time spent by booked by user|Insufficient overlap time)%' THEN 'Not conducted - Student No show'
                WHEN sd.cancel_reason LIKE 'Insufficient time spent by booked with user%'                                  THEN 'Not conducted - Interviewer No show'
                ELSE 'Not conducted'
            END
        ELSE
            CASE sd.one_to_one_status
                WHEN 1 THEN 'Pending Confirmation'
                WHEN 3 THEN 'Interviewer Declined'
                WHEN 4 THEN 'Interviewer Cancelled'
                WHEN 5 THEN 'Student Cancelled'
                WHEN 6 THEN 'Lost'
                WHEN 7 THEN 'Student pending confirmation'
                WHEN 8 THEN 'Interviewer pending confirmation'
                WHEN 9 THEN 'Student declined'
                ELSE 'Unknown'
            END
    END AS session_status,

    CONCAT('https://my.newtonschool.co/play-video/?url=https://d3dyfaf3iutrxo.cloudfront.net/', otvsr.recording) AS lecture_recording_link,

    pt.mentor_join_time, pt.mentor_leave_time,
    pt.student_join_time, pt.student_leave_time,

    CASE
        WHEN pt.student_leave_time IS NOT NULL AND pt.mentor_leave_time IS NOT NULL
             AND pt.mentor_join_time IS NOT NULL AND pt.student_join_time IS NOT NULL
        THEN GREATEST(0,
            EXTRACT(EPOCH FROM (
                LEAST(pt.mentor_leave_time, pt.student_leave_time) -
                GREATEST(pt.mentor_join_time, pt.student_join_time)
            )) / 60
        )
        ELSE 0
    END AS over_lap_time_in_minute,

    -- Using stay_duration from CTE 2 (cleaner, single join)
    COALESCE(EXTRACT(EPOCH FROM pt.mentor_stay_duration)  / 60, 0) AS mentor_time_spent_in_minute,
    COALESCE(EXTRACT(EPOCH FROM pt.student_stay_duration) / 60, 0) AS student_time_spent_in_minute,

    -- Shared columns
    fs.mentor_rating, fs.call_type, fs.communication_rating, fs.self_intro,
    fs.project_explanation, fs.business_acumen, fs.hr_questions,
    fs.student_intent, fs.job_readiness, fs.pr_conversion_weeks, fs.fit_for_placements,

    -- Form 4481 rolled-up scores
    fs.excel, fs.sql, fs.power_bi, fs.pace_speed,

    -- Form 4523 granular scores
    fs.excel_data_loading_handling,
    fs.excel_calculations_1_basic, fs.excel_calculation_2_intermediate,
    fs.excel_analysis_reporting, fs.excel_advanced, fs.excel_overall_proficiency,
    fs.sql_fundamentals_theory, fs.sql_calculation_1_basic,
    fs.sql_calculation_1_core, fs.sql_calculation_2_intermediate,
    fs.sql_calculations_3_advanced, fs.sql_overall_proficiency,
    fs.pbi_data_handling, fs.pbi_basics1, fs.pbi_basic2,
    fs.pbi_analysis_reporting, fs.pbi_advanced, fs.pbi_overall_proficiency

FROM one_to_one_session_data sd
LEFT JOIN one_to_one_participant_times pt ON pt.id = sd.id
LEFT JOIN one_to_one_feedback_scores fs  ON fs.id = sd.id
-- join the deduped recordings CTE (rn = 1) instead of the raw table directly
LEFT JOIN one_to_one_recordings otvsr
    ON otvsr.video_session_object_id = sd.id
    AND otvsr.rn = 1

UNION ALL

-- *** FINAL SELECT: GROUP MEETINGS ***
SELECT
    gbm.id AS session_id,
    gbm.title AS meeting_title,
    gbm.participants_count,
    'Meeting' AS session_type,
    gbm.student_id,
    CONCAT(au2.first_name, ' ', au2.last_name) AS student_name,
    CONCAT(au1.first_name, ' ', au1.last_name) AS mentor_name,
    cc.title AS batch,
    gbm.start_timestamp AS session_start_time,

    CASE gbm.meeting_status
        WHEN 1 THEN
            CASE
                WHEN ROUND(EXTRACT(EPOCH FROM (
                    LEAST(pt.mentor_leave_time, pt.student_leave_time) -
                    GREATEST(pt.mentor_join_time, pt.student_join_time)
                ))::numeric / 60, 0) < 3
                THEN 'No Show'
                ELSE 'Conducted'
            END
        WHEN 2 THEN 'Not Conducted'
        ELSE 'Unknown'
    END AS session_status,

    CONCAT('https://my.newtonschool.co/play-video/?url=https://d3dyfaf3iutrxo.cloudfront.net/', vsvr.recording) AS lecture_recording_link,

    pt.mentor_join_time, pt.mentor_leave_time,
    pt.student_join_time, pt.student_leave_time,

    CASE
        WHEN pt.student_leave_time IS NOT NULL AND pt.mentor_leave_time IS NOT NULL
             AND GREATEST(pt.mentor_join_time, pt.student_join_time) < LEAST(pt.mentor_leave_time, pt.student_leave_time)
        THEN EXTRACT(EPOCH FROM (
            LEAST(pt.mentor_leave_time, pt.student_leave_time) -
            GREATEST(pt.mentor_join_time, pt.student_join_time)
        )) / 60
        ELSE 0
    END AS over_lap_time_in_minute,

    EXTRACT(EPOCH FROM (pt.mentor_leave_time - pt.mentor_join_time)) / 60 AS mentor_time_spent_in_minute,
    EXTRACT(EPOCH FROM (pt.student_leave_time - pt.student_join_time)) / 60 AS student_time_spent_in_minute,

    -- Shared columns
    MAX(CASE WHEN fq.id = 20  THEN fa.text END) AS mentor_rating,
    MAX(CASE WHEN fq.id = 571 THEN fa.text END) AS call_type,
    MAX(CASE WHEN fq.id = 915 THEN fa.text END) AS communication_rating,
    MAX(CASE WHEN fq.id = 916 THEN fa.text END) AS self_intro,
    MAX(CASE WHEN fq.id = 917 THEN fa.text END) AS project_explanation,
    MAX(CASE WHEN fq.id = 918 THEN fa.text END) AS business_acumen,
    MAX(CASE WHEN fq.id = 704 THEN fa.text END) AS hr_questions,
    MAX(CASE WHEN fq.id = 711 THEN fa.text END) AS student_intent,
    MAX(CASE WHEN fq.id = 919 THEN fa.text END) AS job_readiness,
    MAX(CASE WHEN fq.id = 713 THEN fa.text END) AS pr_conversion_weeks,
    MAX(CASE WHEN fq.id = 714 THEN fa.text END) AS fit_for_placements,

    -- Form 4481/4483 rolled-up scores
    MAX(CASE WHEN fq.id = 920 THEN fa.text END) AS excel,
    MAX(CASE WHEN fq.id = 921 THEN fa.text END) AS sql,
    MAX(CASE WHEN fq.id = 922 THEN fa.text END) AS power_bi,
    MAX(CASE WHEN fq.id = 702 THEN fa.text END) AS pace_speed,

    -- Form 4523 granular scores
    MAX(CASE WHEN fq.id = 900 THEN fa.text END) AS excel_data_loading_handling,
    MAX(CASE WHEN fq.id = 901 THEN fa.text END) AS excel_calculations_1_basic,
    MAX(CASE WHEN fq.id = 902 THEN fa.text END) AS excel_calculation_2_intermediate,
    MAX(CASE WHEN fq.id = 903 THEN fa.text END) AS excel_analysis_reporting,
    MAX(CASE WHEN fq.id = 904 THEN fa.text END) AS excel_advanced,
    MAX(CASE WHEN fq.id = 920 THEN fa.text END) AS excel_overall_proficiency,
    MAX(CASE WHEN fq.id = 905 THEN fa.text END) AS sql_fundamentals_theory,
    MAX(CASE WHEN fq.id = 906 THEN fa.text END) AS sql_calculation_1_basic,
    MAX(CASE WHEN fq.id = 907 THEN fa.text END) AS sql_calculation_1_core,
    MAX(CASE WHEN fq.id = 908 THEN fa.text END) AS sql_calculation_2_intermediate,
    MAX(CASE WHEN fq.id = 909 THEN fa.text END) AS sql_calculations_3_advanced,
    MAX(CASE WHEN fq.id = 921 THEN fa.text END) AS sql_overall_proficiency,
    MAX(CASE WHEN fq.id = 910 THEN fa.text END) AS pbi_data_handling,
    MAX(CASE WHEN fq.id = 911 THEN fa.text END) AS pbi_basics1,
    MAX(CASE WHEN fq.id = 912 THEN fa.text END) AS pbi_basic2,
    MAX(CASE WHEN fq.id = 913 THEN fa.text END) AS pbi_analysis_reporting,
    MAX(CASE WHEN fq.id = 914 THEN fa.text END) AS pbi_advanced,
    MAX(CASE WHEN fq.id = 922 THEN fa.text END) AS pbi_overall_proficiency


FROM group_meeting_base_data gbm
INNER JOIN group_meeting_participant_times pt ON pt.id = gbm.id --AND gbm.id = 100424
INNER JOIN courses_course cc  ON gbm.course_id = cc.id
INNER JOIN auth_user au1 ON gbm.mentor_id  = au1.id
INNER JOIN auth_user au2 ON gbm.student_id = au2.id

-- added the group_meeting_base_data id scope (already computed above) on
-- top of the existing content-type filter -- pure narrowing, same result, cheaper
-- (EXPLAIN cost ~28,600 -> ~26,400).
LEFT JOIN (
    SELECT vsr.video_session_object_id, vsr.recording,
           ROW_NUMBER() OVER (PARTITION BY vsr.video_session_object_id ORDER BY vsr.id) AS rn
    FROM video_sessions_videosessionrecording vsr
    WHERE vsr.video_session_content_type_id in (45,475)
      AND vsr.video_session_object_id IN (SELECT id FROM group_meeting_base_data)
) vsvr ON gbm.id = vsvr.video_session_object_id AND vsvr.rn = 1

LEFT JOIN video_sessions_meetingbookedwithuser vbm
    ON vbm.meeting_id = gbm.id AND vbm.user_id = gbm.student_id
LEFT JOIN feedback_feedbackformusermapping ffum
    ON ffum.entity_object_id = vbm.id
    AND ffum.filled_by_id = gbm.mentor_id
    AND ffum.course_id = gbm.course_id
    AND ffum.feedback_form_id IN (4482, 4483, 4523, 4528)
    AND ffum.entity_content_type_id = 475
-- question ID filter added to group meeting ffuqam join (performance)
LEFT JOIN feedback_feedbackformuserquestionanswermapping ffuqam
    ON ffuqam.feedback_form_user_mapping_id = ffum.id
    AND ffuqam.feedback_question_id IN (
        20, 571, 915, 916, 917, 918, 920, 900, 901, 902, 903, 904, 905, 906, 907, 908, 909,
        921, 910, 911, 912, 913, 914, 922, 919, 713, 714, 726
    )
LEFT JOIN feedback_feedbackformuserquestionanswerm2m ffuqam2m
    ON ffuqam2m.feedback_form_user_question_answer_mapping_id = ffuqam.id
LEFT JOIN feedback_feedbackanswer fa  ON fa.id  = ffuqam2m.feedback_answer_id
LEFT JOIN feedback_feedbackquestion fq ON fq.id = ffuqam.feedback_question_id

GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18

ORDER BY session_start_time DESC;
