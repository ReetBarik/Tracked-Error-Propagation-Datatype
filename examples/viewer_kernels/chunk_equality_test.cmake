# ctest script: the chunkable-driver byte-equality guarantee (docs/STREAMING.md
# "within one process").  Runs gen_viewer_fixtures monolithically and with two
# different chunk sizes and asserts all three journals are byte-identical —
# flush_and_clear never resets the callsite id counters, so chunking must not
# change a single byte.
#
# Invoked as:
#   cmake -DDRIVER=<path> -DWORK_DIR=<dir> -P chunk_equality_test.cmake

if(NOT DRIVER OR NOT WORK_DIR)
  message(FATAL_ERROR "usage: cmake -DDRIVER=... -DWORK_DIR=... -P ${CMAKE_CURRENT_LIST_FILE}")
endif()

file(MAKE_DIRECTORY "${WORK_DIR}")

set(common --sample-offset 3 --sample-count 7)

foreach(spec "mono;0" "chunk1;1" "chunk5;5")
  list(GET spec 0 name)
  list(GET spec 1 size)
  execute_process(
    COMMAND "${DRIVER}" --out "${WORK_DIR}/${name}.jsonl" --chunk-size ${size} ${common}
    RESULT_VARIABLE rc OUTPUT_QUIET)
  if(NOT rc EQUAL 0)
    message(FATAL_ERROR "driver failed (${name}, chunk-size ${size}): rc=${rc}")
  endif()
endforeach()

foreach(other chunk1 chunk5)
  execute_process(
    COMMAND ${CMAKE_COMMAND} -E compare_files
            "${WORK_DIR}/mono.jsonl" "${WORK_DIR}/${other}.jsonl"
    RESULT_VARIABLE rc)
  if(NOT rc EQUAL 0)
    message(FATAL_ERROR "chunked journal ${other}.jsonl differs from the monolithic run — "
                        "flush_and_clear broke the byte-equality contract")
  endif()
endforeach()

message(STATUS "chunked journals are byte-identical to the monolithic run")
