import json
import csv
import os
from datetime import datetime


def extract_bulletproof_list(json_file_path, csv_file_path):
    if not os.path.exists(json_file_path):
        print(f"错误: 找不到文件 {json_file_path}")
        return

    try:
        with open(json_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        headers = ['TMDB_ID', '媒体类型', '名称', '季', '集', '格式化标签']
        extracted_rows = []

        for item in data:
            # ==========================================
            # 补丁1：兼容有/无 content 节点的各种畸形数据
            # ==========================================
            content = item.get('content', {})

            tmdb_id = content.get('tmdbId') or item.get('tmdbId') or ''
            item_type = str(content.get('type') or item.get('type') or '').lower()
            title = content.get('title') or content.get('name') or item.get('title') or item.get('name') or '未知媒体'
            status = str(item.get('status', '')).upper()

            if item_type == 'movie':
                if status in ['FINISHED', 'COMPLETED']:
                    extracted_rows.append({
                        'TMDB_ID': tmdb_id, '媒体类型': '电影', '名称': title,
                        '季': '', '集': '', '格式化标签': title
                    })

            elif item_type == 'tv':
                fully_watched_seasons = set()
                for s in item.get('watchedSeasons', []):
                    if isinstance(s, int): fully_watched_seasons.add(s)

                # ==========================================
                # 补丁2：按时间线严格回放事件，防止乱序导致状态错误
                # ==========================================
                activities = item.get('activity', [])
                # 确保事件按发生时间排序，再回放历史
                activities.sort(key=lambda x: x.get('createdAt', ''))

                watched_episodes = set()
                for act in activities:
                    act_type = act.get('type', '')
                    if 'EPISODE_' in act_type:
                        try:
                            act_data = json.loads(act.get('data', '{}'))
                            s_num, e_num = act_data.get('season'), act_data.get('episode')
                            if s_num is not None and e_num is not None:
                                s_num, e_num = int(s_num), int(e_num)
                                if 'ADDED' in act_type:
                                    watched_episodes.add((s_num, e_num))
                                elif 'REMOVED' in act_type:
                                    watched_episodes.discard((s_num, e_num))
                        except (json.JSONDecodeError, ValueError):
                            continue

                filtered_episodes = set()
                for s_num, e_num in watched_episodes:
                    if s_num not in fully_watched_seasons:
                        filtered_episodes.add((s_num, e_num))

                # ==========================================
                # 补丁3：一键 FINISHED 兜底，读取真实总季数防止丢弃后续季
                # ==========================================
                if not fully_watched_seasons and not filtered_episodes and status in ['FINISHED', 'COMPLETED']:
                    total_seasons = content.get('numberOfSeasons', 0)
                    if isinstance(total_seasons, int) and total_seasons > 0:
                        # 比如总共有 5 季，就自动生成 1,2,3,4,5 季全部看完
                        for i in range(1, total_seasons + 1):
                            fully_watched_seasons.add(i)
                    else:
                        fully_watched_seasons.add(1)  # 如果连总季数都没抓到，只能默认保底第 1 季

                for s_num in fully_watched_seasons:
                    extracted_rows.append({
                        'TMDB_ID': tmdb_id, '媒体类型': '剧集', '名称': title,
                        '季': s_num, '集': '全季', '格式化标签': f"{title} 第{s_num}季 (全)"
                    })

                for s_num, e_num in filtered_episodes:
                    extracted_rows.append({
                        'TMDB_ID': tmdb_id, '媒体类型': '剧集', '名称': title,
                        '季': s_num, '集': e_num, '格式化标签': f"{title} S{s_num:02d}E{e_num:02d}"
                    })

        def sort_key(x):
            s = x['季'] if isinstance(x['季'], int) else 0
            e_val = x['集']
            e = -1 if e_val == '全季' else (e_val if isinstance(e_val, int) else 0)
            return (x['媒体类型'], x['名称'], s, e)

        extracted_rows.sort(key=sort_key)

        with open(csv_file_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(extracted_rows)

        print(f"提取完成！共无损提取 {len(extracted_rows)} 条记录。")

    except Exception as e:
        print(f"处理数据时发生异常: {str(e)}")


if __name__ == "__main__":
    extract_bulletproof_list("watcharr-export.json", "watcharr_bulletproof_list.csv")