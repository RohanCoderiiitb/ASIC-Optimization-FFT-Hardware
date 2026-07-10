// =============================================================================
// Pipelined Streaming Address Generation Unit (AGU)
// WITH STALL SUPPORT FOR PIPELINE FLUSHING
// =============================================================================
`timescale 1ns/1ps

module dit_fft_agu_streaming #(
    parameter MAX_N = 1024,
    parameter ADDR_WIDTH = $clog2(MAX_N) + 1
)(
    input  wire                  clk,
    input  wire                  reset,
    input  wire                  stall,       // <--- NEW: Stall to allow pipeline to flush
    input  wire                  start,       // 1-cycle pulse starts the streaming engine
    input  wire [ADDR_WIDTH-1:0] N,           // Runtime N value

    output wire                  stream_en,   // HIGH when valid addresses are being pumped

    output wire [ADDR_WIDTH-1:0] idx_a,       // Address for Port A
    output wire [ADDR_WIDTH-1:0] idx_b,       // Address for Port B
    output wire [ADDR_WIDTH-1:0] k,           // Twiddle factor index
    output wire                  done_stage,  // Asserts combinationally on last element of stage
    output wire                  done_fft,    // Asserts combinationally on last element of FFT
    output reg  [ADDR_WIDTH-1:0] curr_stage   // Current stage (0 to log2(MAX_N)-1)
);
    reg [ADDR_WIDTH-1:0] total_stages;
    
    always @(*) begin
        if      (N == 11'd1024 || N == 11'd0) total_stages = 10'd10;
        else if (N == 11'd512)  total_stages = 10'd9;
        else if (N == 11'd256)  total_stages = 10'd8;
        else if (N == 11'd128)  total_stages = 10'd7;
        else if (N == 11'd64)   total_stages = 10'd6;
        else if (N == 11'd32)   total_stages = 10'd5;
        else if (N == 11'd16)   total_stages = 10'd4;
        else if (N == 11'd8)    total_stages = 10'd3;
        else if (N == 11'd4)    total_stages = 10'd2;
        else if (N == 11'd2)    total_stages = 10'd1;
        else                    total_stages = 10'd3;
    end

    reg                  active;
    reg [ADDR_WIDTH-1:0] group;      
    reg [ADDR_WIDTH-1:0] butterfly;  
    reg [ADDR_WIDTH-1:0] stride;     

    wire [ADDR_WIDTH:0]   group_size   = (stride << 1);
    wire [ADDR_WIDTH-1:0] group_offset = group * group_size;

    assign idx_a = group_offset + butterfly;
    assign idx_b = idx_a + stride;

    wire [ADDR_WIDTH-1:0] shift_amt  = curr_stage + 1;
    wire [ADDR_WIDTH-1:0] num_groups = N >> shift_amt; 
    
    assign k = butterfly * (N >> shift_amt);

    // Stop pumping data and flags if stalled
    assign stream_en  = active && !stall;
    wire last_butterfly = (butterfly == stride - 1);
    wire last_group     = (group == num_groups - 1);
    wire last_stage     = (curr_stage == total_stages - 1);

    assign done_stage = active && !stall && last_butterfly && last_group;
    assign done_fft   = done_stage && last_stage;

    always @(posedge clk or negedge reset) begin
        if (!reset) begin
            active     <= 0;
            curr_stage <= 0;
            group      <= 0;
            butterfly  <= 0;
            stride     <= 1; 
        end
        else if (start) begin 
            active     <= 1;
            curr_stage <= 0;
            group      <= 0;
            butterfly  <= 0;
            stride     <= 1; 
        end
        else if (active && !stall) begin  // <--- STALL APPLIED HERE
            if (last_butterfly) begin
                butterfly <= 0;
                if (last_group) begin
                    group <= 0;
                    if (last_stage) begin
                        active <= 0;
                    end else begin
                        curr_stage <= curr_stage + 1;
                        stride     <= stride << 1; 
                    end
                end else begin
                    group <= group + 1;
                end
            end else begin
                butterfly <= butterfly + 1;
            end
        end
    end
endmodule