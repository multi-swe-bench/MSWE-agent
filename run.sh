python3 multirun.py \
   --model_name gpt4o \
   --cache_task_images True \
   --per_instance_api_calls_limit 50 \
   --pre_build_all_images True \
   --remove_image False \
   --pr_file data/go.jsonl \
   --config_file config/default.yaml  --skip_existing=True \
   --per_instance_cost_limit 5.00 \
   --print_config=False \
   --max_workers_build_image 16